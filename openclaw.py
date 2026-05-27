"""
OpenClaw v5.2.0
─────────────────────────────────────────
변경사항 (v5.1.0 → v5.2.0):
  - 클립 추출 방식 개선: 클러스터 기반으로 변경
    · 이전: 처치 감지 → 즉시 60초 클립 (30s 전후 고정)
    · 이제: 처치들을 전부 수집 → 60초 이내 연속 처치를 하나의 클러스터로 묶음
            클립 = 첫 처치 - 30초 ~ 마지막 처치 + 30초 (가변 길이)
  - 중복 감지 방지: 동일 화면 재감지 3초 dedup

사용법:
  python openclaw.py <영상파일.mkv>
  python openclaw.py <영상파일.mkv> --no-telegram
"""

import cv2
import pytesseract
import subprocess
import os
import sys
import csv
import json
import datetime
from pathlib import Path

# ── 텔레그램 (선택) ───────────────────────────────────────────
try:
    from notify import send_telegram
    TELEGRAM_ENABLED = "--no-telegram" not in sys.argv
except ImportError:
    TELEGRAM_ENABLED = False

DRIVE_ENABLED = False  # Google Drive 업로드 미사용


# ════════════════════════════════════════════════════════════════
#  설정값
# ════════════════════════════════════════════════════════════════

# ROI — 검증된 좌표 (x: 28~58%, y: 62~78%)
ROI_X1_RATIO = 0.28
ROI_X2_RATIO = 0.58
ROI_Y1_RATIO = 0.62
ROI_Y2_RATIO = 0.78

SCAN_INTERVAL_SEC   = 0.5   # 0.5초 간격 스캔
DEDUP_SEC           = 3     # 동일 화면 재감지 방지 (중복 제거)
CLUSTER_GAP_SEC     = 60    # 처치 간격이 이 값 이상이면 별도 클러스터
CLIP_BEFORE_SEC     = 30    # 첫 처치 기준 앞 30초
CLIP_AFTER_SEC      = 30    # 마지막 처치 기준 뒤 30초

KEYWORD             = "탈락"
OCR_LANG            = "kor"
OCR_CONFIG          = "--psm 6"

# 진행상황 공유 파일 (watcher.py에서 읽음)
PROGRESS_FILE = Path.home() / "Desktop" / "OpenClaw-project" / ".progress.json"


# ════════════════════════════════════════════════════════════════
#  타임코드 유틸
# ════════════════════════════════════════════════════════════════

def seconds_to_timecode(seconds: float) -> str:
    """초 → HH:MM:SS:FF (30fps 기준) 타임코드"""
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    ff = int((seconds - int(seconds)) * 30)
    return f"{h:02d}:{m:02d}:{s:02d}:{ff:02d}"

def seconds_to_hhmmss(seconds: float) -> str:
    """초 → HH:MM:SS (가독용)"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ════════════════════════════════════════════════════════════════
#  클립 추출
# ════════════════════════════════════════════════════════════════

def extract_clip(src_path: str, start_sec: float, end_sec: float,
                 out_path: str) -> bool:
    """
    ffmpeg으로 개별 클립 추출.
    -ss를 input 앞에 두면 keyframe seek로 빠르고,
    -accurate_seek로 정밀 컷 보정.
    """
    duration = end_sec - start_sec
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(max(0, start_sec)),
        "-i", src_path,
        "-t", str(duration),
        "-c:v", "libx264", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        out_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0


# ════════════════════════════════════════════════════════════════
#  타임코드 기록 저장
# ════════════════════════════════════════════════════════════════

def save_timecode_log(detections: list, out_dir: Path, video_name: str):
    """
    detections: [{"index": 1, "detect_sec": 123.4, "clip_start": 93.4,
                   "clip_end": 153.4, "clip_file": "..._kill_01.mp4"}, ...]

    저장 형식:
      1. timecode_log.csv  — 프리미어에서 열기 쉬운 CSV
      2. timecode_log.json — 추후 자동화용
      3. markers.edl       — 프리미어 마커 가이드 (텍스트 참고용)
    """
    if not detections:
        return

    # ── 1. CSV ───────────────────────────────────────────────
    csv_path = out_dir / "timecode_log.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([
            "번호",
            "감지 시각 (HH:MM:SS)",
            "감지 타임코드 (HH:MM:SS:FF)",
            "감지 초",
            "클립 시작 (HH:MM:SS)",
            "클립 종료 (HH:MM:SS)",
            "클립 시작 초",
            "클립 종료 초",
            "클립 파일명",
            "원본 파일"
        ])
        for d in detections:
            writer.writerow([
                d["index"],
                seconds_to_hhmmss(d["detect_sec"]),
                seconds_to_timecode(d["detect_sec"]),
                f"{d['detect_sec']:.2f}",
                seconds_to_hhmmss(d["clip_start"]),
                seconds_to_hhmmss(d["clip_end"]),
                f"{d['clip_start']:.2f}",
                f"{d['clip_end']:.2f}",
                d["clip_file"],
                video_name
            ])
    print(f"  [LOG] CSV 저장 → {csv_path.name}")

    # ── 2. JSON ──────────────────────────────────────────────
    json_path = out_dir / "timecode_log.json"
    payload = {
        "source_video": video_name,
        "generated_at": datetime.datetime.now().isoformat(),
        "total_detections": len(detections),
        "detections": [
            {
                **d,
                "detect_timecode": seconds_to_timecode(d["detect_sec"]),
                "detect_hhmmss":   seconds_to_hhmmss(d["detect_sec"]),
                "clip_start_hhmmss": seconds_to_hhmmss(d["clip_start"]),
                "clip_end_hhmmss":   seconds_to_hhmmss(d["clip_end"]),
            }
            for d in detections
        ]
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  [LOG] JSON 저장 → {json_path.name}")

    # ── 3. 프리미어 마커 가이드 텍스트 ──────────────────────
    edl_path = out_dir / "premiere_markers.txt"
    with open(edl_path, "w", encoding="utf-8") as f:
        f.write(f"# OpenClaw — 프리미어 프로 마커 가이드\n")
        f.write(f"# 원본 파일: {video_name}\n")
        f.write(f"# 생성일시: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"# 총 탈락 감지: {len(detections)}건\n")
        f.write("─" * 60 + "\n\n")
        f.write("[ 프리미어 프로 사용법 ]\n")
        f.write("1. 원본 영상을 타임라인에 올린다\n")
        f.write("2. 아래 타임코드 위치로 이동 (단축키: Ctrl+G → 타임코드 입력)\n")
        f.write("3. M키로 마커 추가, 클립명 참고\n\n")
        f.write("─" * 60 + "\n\n")
        for d in detections:
            f.write(f"탈락 #{d['index']:02d}\n")
            f.write(f"  감지 위치  : {seconds_to_hhmmss(d['detect_sec'])}  "
                    f"({seconds_to_timecode(d['detect_sec'])})\n")
            f.write(f"  클립 구간  : {seconds_to_hhmmss(d['clip_start'])} ~ "
                    f"{seconds_to_hhmmss(d['clip_end'])}\n")
            f.write(f"  클립 파일  : {d['clip_file']}\n\n")
    print(f"  [LOG] 프리미어 마커 가이드 → {edl_path.name}")


# ════════════════════════════════════════════════════════════════
#  썸네일 저장
# ════════════════════════════════════════════════════════════════

def save_thumbnail(frame, out_dir: Path, index: int):
    thumb_path = out_dir / f"thumb_{index:02d}.jpg"
    cv2.imwrite(str(thumb_path), frame)
    return thumb_path.name


# ════════════════════════════════════════════════════════════════
#  메인 처리
# ════════════════════════════════════════════════════════════════

def process_video(video_path: str):
    src = Path(video_path)
    if not src.exists():
        print(f"[ERROR] 파일 없음: {video_path}")
        sys.exit(1)

    # 결과물 폴더: ~/Desktop/OpenClaw-project/output/원본명_openclaw/
    OUTPUT_BASE = Path.home() / "Desktop" / "OpenClaw-project" / "output"
    OUTPUT_BASE.mkdir(parents=True, exist_ok=True)
    out_dir = OUTPUT_BASE / f"{src.stem}_openclaw"
    out_dir.mkdir(exist_ok=True)
    print(f"\n{'═'*55}")
    print(f"  OpenClaw v5.2.0")
    print(f"  원본 : {src.name}")
    print(f"  출력 : {out_dir}")
    print(f"{'═'*55}\n")

    cap = cv2.VideoCapture(str(src))
    if not cap.isOpened():
        print("[ERROR] 영상을 열 수 없음")
        sys.exit(1)

    fps        = cap.get(cv2.CAP_PROP_FPS) or 30
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    total_sec  = total_frames / fps

    print(f"  FPS: {fps:.2f}  |  총 길이: {seconds_to_hhmmss(total_sec)}")
    print(f"  스캔 간격: {SCAN_INTERVAL_SEC}s  |  클러스터 간격: {CLUSTER_GAP_SEC}s\n")

    # ── Phase 1: 처치 타임스탬프 전체 수집 ──────────────────────
    # DEDUP_SEC(3초) dedup만 적용 — 쿨타임 없이 전부 기록
    raw_kills = []   # [(detect_sec, frame_copy), ...]
    raw_count = 0
    i = 0
    dedup_frames  = int(fps * DEDUP_SEC)
    step_frames   = int(fps * SCAN_INTERVAL_SEC)

    print("  [1/2] 탈락 장면 스캔 중...\n")

    while i < total_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if not ret:
            break

        h, w = frame.shape[:2]
        x1 = int(w * ROI_X1_RATIO)
        x2 = int(w * ROI_X2_RATIO)
        y1 = int(h * ROI_Y1_RATIO)
        y2 = int(h * ROI_Y2_RATIO)

        roi  = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(roi, 0, 255,
                                  cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        txt = pytesseract.image_to_string(binary, lang=OCR_LANG,
                                          config=OCR_CONFIG)

        # 진행률 파일 주기적 업데이트 (20 스텝마다 ≈ 10초)
        if i % (step_frames * 20) == 0:
            try:
                with open(str(PROGRESS_FILE), "w", encoding="utf-8") as _pf:
                    json.dump({
                        "video": src.name,
                        "progress_pct": round(i / total_frames * 100, 1) if total_frames else 0,
                        "current_hhmmss": seconds_to_hhmmss(i / fps),
                        "total_hhmmss": seconds_to_hhmmss(total_sec),
                        "kill_count": raw_count,
                        "updated_at": datetime.datetime.now().isoformat()
                    }, _pf, ensure_ascii=False)
            except Exception:
                pass

        if KEYWORD in txt:
            raw_count += 1
            detect_sec = i / fps
            raw_kills.append((detect_sec, frame.copy()))
            print(f"    감지 #{raw_count:02d}: {seconds_to_hhmmss(detect_sec)}"
                  f"  ({seconds_to_timecode(detect_sec)})")
            i += dedup_frames   # 동일 화면 재감지 방지 (3초)
        else:
            i += step_frames

    cap.release()

    # ── Phase 2: 클러스터 묶기 ───────────────────────────────────
    # 처치 간격이 CLUSTER_GAP_SEC(60초) 미만이면 같은 클러스터
    clusters = []   # [[(sec, frame), ...], ...]
    if raw_kills:
        current = [raw_kills[0]]
        for j in range(1, len(raw_kills)):
            prev_sec = current[-1][0]
            curr_sec = raw_kills[j][0]
            if curr_sec - prev_sec < CLUSTER_GAP_SEC:
                current.append(raw_kills[j])   # 같은 클러스터
            else:
                clusters.append(current)        # 새 클러스터 시작
                current = [raw_kills[j]]
        clusters.append(current)

    print(f"\n  → 감지된 처치: {raw_count}건  |  클러스터: {len(clusters)}개\n")

    # ── Phase 3: 클러스터당 클립 1개 추출 ────────────────────────
    # 클립 = 클러스터 첫 처치 - 30초  ~  마지막 처치 + 30초
    detections = []
    kill_count = 0

    print("  [2/2] 클립 추출 중...\n")

    for cluster in clusters:
        kill_count += 1
        first_sec  = cluster[0][0]
        last_sec   = cluster[-1][0]
        thumb_frame = cluster[0][1]    # 첫 처치 프레임을 썸네일로

        clip_start = max(0.0, first_sec - CLIP_BEFORE_SEC)
        clip_end   = min(total_sec, last_sec  + CLIP_AFTER_SEC)
        clip_dur   = clip_end - clip_start

        clip_name  = f"{src.stem}_kill_{kill_count:02d}.mp4"
        clip_path  = out_dir / clip_name
        thumb_name = save_thumbnail(thumb_frame, out_dir, kill_count)

        kills_in_cluster = len(cluster)
        print(f"  ✂  클러스터 #{kill_count:02d}  "
              f"처치 {kills_in_cluster}건  "
              f"({seconds_to_hhmmss(first_sec)} ~ {seconds_to_hhmmss(last_sec)})")
        print(f"       클립 구간: {seconds_to_hhmmss(clip_start)} ~ "
              f"{seconds_to_hhmmss(clip_end)}  ({clip_dur:.0f}초)")
        print(f"       추출 중... ", end="", flush=True)

        ok = extract_clip(str(src), clip_start, clip_end, str(clip_path))
        print("완료 ✓" if ok else "실패 ✗")

        detection = {
            "index":            kill_count,
            "kills_in_cluster": kills_in_cluster,
            "detect_sec":       round(first_sec, 2),   # 호환성: 첫 처치 시각
            "first_kill_sec":   round(first_sec, 2),
            "last_kill_sec":    round(last_sec,  2),
            "clip_start":       round(clip_start, 2),
            "clip_end":         round(clip_end,   2),
            "clip_file":        clip_name,
            "thumb_file":       thumb_name,
        }
        detections.append(detection)

    # ── 타임코드 로그 저장 ────────────────────────────────────
    print(f"\n{'─'*55}")
    print(f"  총 탈락 감지: {raw_count}건  →  클립 {kill_count}개 추출")
    if kill_count > 0:
        save_timecode_log(detections, out_dir, src.name)
        print(f"\n  📂 결과물 위치: {out_dir}")
        print(f"  ├─ 클립 파일   : *_kill_XX.mp4 ({kill_count}개)")
        print(f"  ├─ 썸네일      : thumb_XX.jpg ({kill_count}개)")
        print(f"  ├─ timecode_log.csv   ← 프리미어에서 바로 열기")
        print(f"  ├─ timecode_log.json  ← 자동화용")
        print(f"  └─ premiere_markers.txt  ← 마커 가이드")
    else:
        print("  탈락 장면이 감지되지 않았습니다.")
    print(f"{'═'*55}\n")


# ════════════════════════════════════════════════════════════════
#  진입점
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python openclaw.py <영상파일.mkv> [--no-telegram]")
        sys.exit(1)

    process_video(sys.argv[1])
