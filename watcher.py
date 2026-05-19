"""
watcher.py — OpenClaw 로컬 네트워크 감시 데몬 v3.0
─────────────────────────────────────────────────────────────────
변경사항 (v2 → v3):
  - Google Drive 완전 제거
  - /Volumes/영상 녹화2/ 로컬 네트워크 경로 직접 감시
  - 새 파일(최신순) 우선 처리
  - 텔레그램 선택 방식 유지
  - 결과물은 맥미니 로컬에 저장 + 텔레그램 알림

흐름:
  [/Volumes/영상 녹화2/ 새 .mkv 감지 (최신순)]
       ↓
  [텔레그램으로 목록 전송]
    🎬 새 영상 3개 발견!
    1. 2026-05-15 21-32-54.mkv (8.5GB) ← 최신
    2. 2026-05-14 22-57-28.mkv (8.5GB)
    3. 2026-05-13 01-32-08.mkv (8.5GB)
    처리할 번호를 답장해주세요 (예: 1 3 또는 전체)
       ↓
  [사용자 답장]
       ↓
  [openclaw.py 실행 → 결과물 로컬 저장 → 텔레그램 알림]

사용법:
  python watcher.py            # 상시 실행
  python watcher.py --once     # 1회 스캔 (테스트)
  python watcher.py --poll 30  # 폴링 간격 30초
"""

import os
import sys
import time
import json
import logging
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("[ERROR] pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ════════════════════════════════════════════════════════════════
#  설정
# ════════════════════════════════════════════════════════════════

BASE_DIR      = Path.home() / "Desktop" / "OpenClaw-project"
OUTPUT_DIR    = BASE_DIR / "output"       # 결과물 저장 경로
PROCESSED_LOG = BASE_DIR / ".processed_files.json"

# 감시할 네트워크 경로 (송출PC 공유 폴더)
WATCH_PATH = Path("/Volumes/영상 녹화2")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SEC = 30     # 감시 간격 (초)
TELEGRAM_POLL_SEC = 3      # 텔레그램 답장 확인 간격
SELECTION_TIMEOUT = 300    # 5분 내 선택 없으면 다음 스캔까지 대기

# openclaw.py 가 주기적으로 갱신하는 진행 파일
PROGRESS_FILE = BASE_DIR / ".progress.json"

# 현재 배치 진행 상황 (전역)
_batch: dict = {
    "total": 0,
    "done": 0,
    "current_file": None,
    "current_start": None,
    "done_files": [],
}


# ════════════════════════════════════════════════════════════════
#  로거
# ════════════════════════════════════════════════════════════════

BASE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "watcher.log", encoding="utf-8")
    ]
)
log = logging.getLogger("watcher")


# ════════════════════════════════════════════════════════════════
#  처리 완료 목록
# ════════════════════════════════════════════════════════════════

def load_processed() -> set:
    if PROCESSED_LOG.exists():
        data = json.load(open(PROCESSED_LOG, encoding="utf-8"))
        return set(data.get("processed_paths", []))
    return set()

def save_processed(processed: set):
    with open(PROCESSED_LOG, "w", encoding="utf-8") as f:
        json.dump({
            "processed_paths": list(processed),
            "updated_at": datetime.now().isoformat()
        }, f, ensure_ascii=False, indent=2)


# ════════════════════════════════════════════════════════════════
#  텔레그램
# ════════════════════════════════════════════════════════════════

def send_telegram(message: str) -> bool:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("텔레그램 토큰/채팅ID 미설정")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML"
            },
            timeout=10
        )
        return resp.status_code == 200
    except Exception as e:
        log.error(f"텔레그램 전송 실패: {e}")
        return False

def get_last_update_id() -> int:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"limit": 1},
            timeout=10
        )
        updates = resp.json().get("result", [])
        if updates:
            return updates[-1]["update_id"]
    except Exception:
        pass
    return 0

def get_latest_reply(after_update_id: int) -> tuple:
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": after_update_id + 1, "timeout": 2},
            timeout=10
        )
        updates = resp.json().get("result", [])
        if not updates:
            return None, after_update_id
        last      = updates[-1]
        update_id = last["update_id"]
        text      = last.get("message", {}).get("text", "")
        return text.strip(), update_id
    except Exception as e:
        log.error(f"텔레그램 수신 실패: {e}")
        return None, after_update_id


# ════════════════════════════════════════════════════════════════
#  네트워크 경로 감시
# ════════════════════════════════════════════════════════════════

def check_mount() -> bool:
    """네트워크 드라이브 마운트 상태 확인"""
    if not WATCH_PATH.exists():
        log.warning(f"  경로 없음: {WATCH_PATH}")
        log.warning("  Finder → 네트워크 → 172.30.1.22 → 영상 녹화2 마운트 확인")
        send_telegram(
            f"⚠️ 네트워크 드라이브 연결 끊김\n"
            f"📂 {WATCH_PATH}\n"
            f"Finder에서 다시 연결해주세요"
        )
        return False
    return True

def scan_new_files(processed: set) -> list:
    """
    감시 경로에서 새 .mkv 파일 목록 반환.
    최신 파일(수정 시간 기준) 우선 정렬.
    """
    if not check_mount():
        return []

    all_mkv = sorted(
        WATCH_PATH.glob("*.mkv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True   # 최신 파일 먼저
    )

    new_files = []
    for p in all_mkv:
        path_str = str(p)
        if path_str not in processed:
            size_gb = p.stat().st_size / (1024 ** 3)
            mtime   = datetime.fromtimestamp(p.stat().st_mtime)
            new_files.append({
                "path":     path_str,
                "name":     p.name,
                "size_gb":  size_gb,
                "mtime":    mtime.strftime("%m/%d %H:%M"),
                "path_obj": p
            })

    return new_files


# ════════════════════════════════════════════════════════════════
#  파일 선택 (텔레그램 대화)
# ════════════════════════════════════════════════════════════════

def wait_for_selection(new_files: list) -> list:
    """
    텔레그램으로 파일 목록 전송 후 사용자 답장 대기.
    Returns: 선택된 파일 info 리스트
    """
    msg  = f"🎬 <b>새 영상 {len(new_files)}개 발견!</b>\n"
    msg += f"<i>(최신 순 정렬)</i>\n\n"
    for idx, f in enumerate(new_files, 1):
        msg += f"<b>{idx}.</b> {f['name']}\n"
        msg += f"     📅 {f['mtime']}  💾 {f['size_gb']:.1f}GB\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━\n"
    msg += "처리할 번호를 답장해주세요\n"
    msg += "예) <code>1</code>  <code>1 3</code>  <code>전체</code>\n"
    msg += "건너뛰려면 <code>스킵</code>"

    send_telegram(msg)
    log.info("  텔레그램 목록 전송 완료 — 답장 대기 중...")

    base_update_id = get_last_update_id()
    elapsed = 0

    while elapsed < SELECTION_TIMEOUT:
        time.sleep(TELEGRAM_POLL_SEC)
        elapsed += TELEGRAM_POLL_SEC

        text, new_update_id = get_latest_reply(base_update_id)

        if text is None or new_update_id == base_update_id:
            # 남은 시간 중간 알림 (2분 경과 시)
            if elapsed == 120:
                send_telegram(
                    f"⏳ 아직 선택 대기 중...\n"
                    f"번호 또는 '전체' / '스킵'으로 답장해주세요\n"
                    f"({(SELECTION_TIMEOUT - elapsed) // 60}분 후 자동 종료)"
                )
            continue

        base_update_id = new_update_id
        text = text.strip()
        log.info(f"  답장 수신: '{text}'")

        # 스킵
        if text.lower() in ("스킵", "skip", "건너뛰기"):
            send_telegram("⏭ 스킵했습니다.\n다음 스캔 때 다시 알림드릴게요.")
            return []

        # 전체 선택
        if text.lower() in ("전체", "all", "0"):
            send_telegram(f"✅ 전체 {len(new_files)}개 선택\n처리를 시작합니다!")
            return new_files

        # 번호 파싱
        try:
            nums = [
                int(x) for x in text.replace(",", " ").split()
                if x.replace(",", "").isdigit()
            ]
            if not nums:
                send_telegram("⚠️ 숫자 또는 '전체' / '스킵'으로 입력해주세요")
                continue

            selected, invalid = [], []
            for n in nums:
                if 1 <= n <= len(new_files):
                    # 중복 선택 방지
                    if new_files[n - 1] not in selected:
                        selected.append(new_files[n - 1])
                else:
                    invalid.append(n)

            if invalid:
                send_telegram(
                    f"⚠️ 없는 번호: {invalid}\n"
                    f"1~{len(new_files)} 사이로 입력해주세요"
                )
                continue

            if selected:
                names = "\n".join(f"  • {f['name']}" for f in selected)
                send_telegram(
                    f"✅ {len(selected)}개 선택\n{names}\n\n처리를 시작합니다!"
                )
                return selected

        except ValueError:
            send_telegram("⚠️ 숫자 또는 '전체' / '스킵'으로 입력해주세요")
            continue

    # 타임아웃
    log.warning(f"  {SELECTION_TIMEOUT}초 내 답장 없음 — 다음 스캔까지 대기")
    send_telegram(
        f"⏰ {SELECTION_TIMEOUT // 60}분 내 선택이 없어 대기를 종료합니다\n"
        f"다음 스캔 때 다시 알림드릴게요"
    )
    return []


# ════════════════════════════════════════════════════════════════
#  진행상황 조회
# ════════════════════════════════════════════════════════════════

def send_progress_status():
    """텔레그램으로 현재 배치 진행상황 전송."""
    if _batch["total"] == 0:
        send_telegram("📊 현재 처리 중인 작업이 없습니다")
        return

    msg  = "📊 <b>OpenClaw 진행상황</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━\n"
    msg += f"✅ 완료: {_batch['done']}/{_batch['total']}개\n"

    if _batch["current_file"]:
        elapsed  = time.time() - _batch["current_start"]
        elapsed_str = f"{int(elapsed // 60)}분 {int(elapsed % 60)}초"
        msg += f"🔄 처리 중: <code>{_batch['current_file']}</code>\n"
        msg += f"⏱ 경과: {elapsed_str}\n"

        # openclaw.py 가 기록한 진행 파일 읽기
        if PROGRESS_FILE.exists():
            try:
                with open(PROGRESS_FILE, encoding="utf-8") as f:
                    prog = json.load(f)
                pct   = prog.get("progress_pct", 0)
                cur   = prog.get("current_hhmmss", "")
                total = prog.get("total_hhmmss", "")
                kills = prog.get("kill_count", 0)
                msg += f"📈 스캔: {pct:.0f}%  ({cur} / {total})\n"
                msg += f"✂️ 탈락 감지: {kills}개\n"
            except Exception:
                pass

    done_files = _batch["done_files"]
    if done_files:
        show  = done_files[-3:]
        more  = len(done_files) - len(show)
        msg  += "📝 완료 목록:\n"
        if more > 0:
            msg += f"  … 외 {more}개\n"
        for name in show:
            msg += f"  ✓ {name}\n"

    send_telegram(msg)


# ════════════════════════════════════════════════════════════════
#  OpenClaw 실행 (비차단 — 처리 중 텔레그램 명령 수신 가능)
# ════════════════════════════════════════════════════════════════

def run_openclaw(video_path: Path) -> bool:
    script = BASE_DIR / "openclaw.py"
    if not script.exists():
        log.error(f"openclaw.py 없음: {script}")
        send_telegram(f"❌ openclaw.py 없음\n{script}")
        return False

    # 배치 진행 상황 업데이트
    _batch["current_file"]  = video_path.name
    _batch["current_start"] = time.time()
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    log.info(f"  🦞 OpenClaw 실행: {video_path.name}")
    send_telegram(
        f"🦞 OpenClaw 분석 시작\n"
        f"📁 {video_path.name}\n"
        f"📊 {_batch['done'] + 1}/{_batch['total']}번째"
    )

    # Popen — 비차단으로 실행해 텔레그램 명령 수신 유지
    process = subprocess.Popen(
        [sys.executable, str(script), str(video_path)],
        cwd=str(BASE_DIR)
    )
    poll_update_id = get_last_update_id()

    while process.poll() is None:
        time.sleep(5)
        text, poll_update_id = get_latest_reply(poll_update_id)
        if text:
            cmd = text.strip()
            if cmd in ("진행상황", "진행", "상태", "/status", "status"):
                send_progress_status()
            # 그 외 명령은 무시 (처리 완료 후 main 루프가 처리)

    ok = process.returncode == 0

    # 배치 상태 갱신
    _batch["done"] += 1
    _batch["current_file"]  = None
    _batch["current_start"] = None

    result_dir = video_path.parent / f"{video_path.stem}_openclaw"
    if ok:
        clip_count = len(list(result_dir.glob("*.mp4"))) if result_dir.exists() else 0
        _batch["done_files"].append(video_path.name)
        send_telegram(
            f"✅ 분석 완료  ({_batch['done']}/{_batch['total']})\n"
            f"📁 {video_path.name}\n"
            f"✂️ 탈락 클립 {clip_count}개 추출\n"
            f"📂 결과물: {result_dir}"
        )
    else:
        send_telegram(f"❌ 분석 실패\n📁 {video_path.name}")

    return ok


# ════════════════════════════════════════════════════════════════
#  1회 스캔
# ════════════════════════════════════════════════════════════════

def scan_once(processed: set) -> set:
    log.info(f"── 감시 스캔: {WATCH_PATH} ──")
    new_files = scan_new_files(processed)

    if not new_files:
        log.info("  새 파일 없음")
        return processed

    log.info(f"  새 파일 {len(new_files)}개 발견")
    for f in new_files:
        log.info(f"  → {f['name']}  ({f['size_gb']:.1f}GB)  {f['mtime']}")

    # 텔레그램으로 선택 요청
    selected = wait_for_selection(new_files)

    if not selected:
        log.info("  선택 없음 — 건너뜀")
        return processed

    # 배치 진행 초기화
    _batch["total"]      = len(selected)
    _batch["done"]       = 0
    _batch["done_files"] = []

    # 선택된 파일 순서대로 처리
    for fi in selected:
        video_path = fi["path_obj"]
        log.info(f"\n  처리 시작: {fi['name']}")
        try:
            ok = run_openclaw(video_path)
        except Exception as e:
            log.error(f"  ✗ 실패: {fi['name']} — {e}")
            send_telegram(f"❌ 처리 실패\n📁 {fi['name']}\n오류: {e}")
            continue

        # 성공/실패 무관하게 processed에 추가 (재처리 방지)
        # 재처리 원하면 .processed_files.json 에서 해당 경로 삭제
        processed.add(fi["path"])
        save_processed(processed)
        log.info(f"  ✓ 완료 기록: {fi['name']}")

    return processed


# ════════════════════════════════════════════════════════════════
#  메인
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
#  "편집 실행" 명령 처리 — 최신 파일 즉시 처리
# ════════════════════════════════════════════════════════════════

def run_latest_file():
    """
    텔레그램에서 '편집 실행' 수신 시:
    /Volumes/영상 녹화2/ 에서 가장 최근 .mkv 파일을 즉시 처리.
    """
    if not WATCH_PATH.exists():
        send_telegram(
            f"⚠️ 네트워크 드라이브 연결 끊김\n"
            f"📂 {WATCH_PATH}\n"
            f"Finder에서 다시 연결해주세요"
        )
        return

    all_mkv = sorted(
        WATCH_PATH.glob("*.mkv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )

    if not all_mkv:
        send_telegram("⚠️ 감시 경로에 .mkv 파일이 없습니다")
        return

    latest = all_mkv[0]
    size_gb = latest.stat().st_size / (1024 ** 3)
    mtime = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%m/%d %H:%M")

    send_telegram(
        f"▶️ 편집 실행 — 최신 파일 처리 시작\n"
        f"📁 {latest.name}\n"
        f"📅 {mtime}  💾 {size_gb:.1f}GB"
    )
    log.info(f"  [편집 실행] 최신 파일 처리: {latest.name}")

    _batch["total"]      = 1
    _batch["done"]       = 0
    _batch["done_files"] = []

    try:
        run_openclaw(latest)
    except Exception as e:
        log.error(f"  ✗ 편집 실행 실패: {e}")
        send_telegram(f"❌ 편집 실행 실패\n오류: {e}")


def main():
    parser = argparse.ArgumentParser(description="OpenClaw Watcher v3")
    parser.add_argument("--once",  action="store_true", help="1회 스캔 후 종료")
    parser.add_argument("--poll",  type=int, default=POLL_INTERVAL_SEC,
                        help=f"폴링 간격(초) [기본: {POLL_INTERVAL_SEC}]")
    parser.add_argument("--reset", action="store_true",
                        help="처리 완료 목록 초기화 (전체 재처리)")
    args = parser.parse_args()

    log.info("═" * 48)
    log.info("  OpenClaw Watcher v3.1 시작")
    log.info(f"  감시 경로  : {WATCH_PATH}")
    log.info(f"  결과물 경로: {OUTPUT_DIR}")
    log.info(f"  폴링 간격  : {args.poll}초")
    log.info("═" * 48)

    # 처리 목록 초기화
    if args.reset:
        if PROCESSED_LOG.exists():
            PROCESSED_LOG.unlink()
        log.info("  처리 완료 목록 초기화됨\n")

    # 네트워크 경로 확인 (--once 제외)
    if not args.once and not WATCH_PATH.exists():
        log.warning(f"  감시 경로 없음 (나중에 마운트 시 자동 재시도): {WATCH_PATH}")

    processed = load_processed()
    log.info(f"  기처리 파일: {len(processed)}개\n")

    send_telegram(
        f"🦞 OpenClaw Watcher v3.1 시작!\n"
        f"📂 감시 중: {WATCH_PATH.name}\n"
        f"⏱ 스캔 간격: {args.poll}초\n"
        f"💬 '편집 실행' 전송 시 최신 파일 즉시 처리"
    )

    if args.once:
        scan_once(processed)
        log.info("  [--once] 완료")
        return

    # 텔레그램 update_id 초기화 (기존 메시지 무시)
    last_update_id = get_last_update_id()

    try:
        while True:
            # ── 텔레그램 명령 확인 ──────────────────────────────
            text, last_update_id = get_latest_reply(last_update_id)
            if text:
                cmd = text.strip()
                log.info(f"  [텔레그램 명령] '{cmd}'")
                if cmd in ("편집 실행", "편집실행", "/edit", "edit"):
                    run_latest_file()
                elif cmd in ("진행상황", "진행", "/progress", "progress"):
                    send_progress_status()
                elif cmd in ("상태", "status", "/status"):
                    mounted = WATCH_PATH.exists()
                    send_telegram(
                        f"📡 OpenClaw 상태\n"
                        f"{'✅' if mounted else '❌'} 네트워크 드라이브: "
                        f"{'연결됨' if mounted else '연결 끊김'}\n"
                        f"📝 누적 처리 파일: {len(processed)}개"
                    )
                # 그 외 메시지는 무시 (wait_for_selection에서 처리)

            # ── 정기 스캔 ────────────────────────────────────────
            processed = scan_once(processed)
            log.info(f"  {args.poll}초 대기...\n")
            time.sleep(args.poll)

    except KeyboardInterrupt:
        log.info("\n  watcher 종료")
        send_telegram("🛑 OpenClaw Watcher 종료")


if __name__ == "__main__":
    main()
