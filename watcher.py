"""
watcher.py — OpenClaw 로컬 네트워크 감시 데몬 v3.3
─────────────────────────────────────────────────────────────────
변경사항 (v3.2 → v3.3):
  - 파일 선택 형식 변경:
      단일: 숫자만 입력 (예: 2)
      다중: 괄호로 묶어 입력 (예: (1 3 5))
      전체: 전체
      건너뜀: /skip
  - 새 명령어 추가:
      /stop     — 현재 처리 중인 작업 즉시 중단
      /cancel   — /stop 동의어
      /queue    — 현재 처리 대기열 확인
  - 도움말 메시지 개선 (선택 규칙 포함)

사용법:
  python watcher.py            # 상시 실행
  python watcher.py --once     # 1회 스캔 (테스트)
  python watcher.py --poll N   # 폴링 간격 N초
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
OUTPUT_DIR    = BASE_DIR / "output"
PROCESSED_LOG = BASE_DIR / ".processed_files.json"

WATCH_PATH = Path("/Volumes/영상 녹화2")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

POLL_INTERVAL_SEC  = 3600   # 파일 감시 주기: 1시간
CMD_POLL_SEC       = 5      # 텔레그램 명령 체크 주기: 5초
TELEGRAM_POLL_SEC  = 3      # 선택 대기 중 답장 확인 주기
SELECTION_TIMEOUT  = 300    # 파일 선택 대기 시간: 5분

PROGRESS_FILE = BASE_DIR / ".progress.json"

_batch: dict = {
    "total": 0,
    "done": 0,
    "current_file": None,
    "current_start": None,
    "done_files": [],
}

# 처리 중단 플래그 (run_openclaw 루프에서 확인)
_stop_requested = False


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
#  명령어 정규화
#  "/Status", "STATUS", "상태" 등 모두 통일된 키로 변환
# ════════════════════════════════════════════════════════════════

def normalize_cmd(text: str) -> str:
    """
    '/도움말', '도움말', '/help', 'HELP' → 'help' 형태로 정규화.
    / 접두사 제거 후 소문자 변환. 한국어 별칭 매핑.
    """
    t = text.strip().lstrip("/").lower()
    aliases = {
        # 도움말
        "도움말": "help", "명령어": "help",
        # 상태
        "상태": "status", "상태확인": "status",
        # 진행상황
        "진행상황": "progress", "진행": "progress",
        # 편집 실행
        "편집실행": "edit", "편집 실행": "edit",
        # 파일 목록
        "파일목록": "list", "목록": "list", "새파일": "list",
        # 즉시 스캔
        "스캔": "scan", "즉시스캔": "scan",
        # 초기화
        "초기화": "reset",
        # 스킵
        "스킵": "skip", "건너뛰기": "skip",
        # 전체
        "전체": "all",
        # 중단
        "중단": "stop", "취소": "stop", "cancel": "stop",
        "처리중단": "stop", "처리취소": "stop",
        # 대기열
        "대기열": "queue", "대기": "queue",
    }
    return aliases.get(t, t)


# ════════════════════════════════════════════════════════════════
#  명령어: 도움말
# ════════════════════════════════════════════════════════════════

def send_help():
    msg = (
        "🦞 <b>OpenClaw 명령어 목록</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📋 <b>파일 관리</b>\n"
        "  /list       — 미처리 파일 목록 보기\n"
        "  /scan       — 지금 즉시 폴더 스캔\n"
        "  /edit       — 최신 파일 즉시 처리\n\n"
        "📊 <b>상태 확인</b>\n"
        "  /status     — 드라이브 연결 + 처리 현황\n"
        "  /progress   — 현재 분석 진행률\n"
        "  /queue      — 현재 처리 대기열 확인\n\n"
        "🛑 <b>제어</b>\n"
        "  /stop       — 현재 처리 작업 즉시 중단\n"
        "  /skip       — 파일 선택 건너뜀\n\n"
        "⚙️ <b>기타</b>\n"
        "  /help       — 이 도움말\n"
        "  /reset      — 처리 목록 초기화 (재처리)\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>파일 선택 방법</b>\n"
        "  단일: <code>2</code>\n"
        "  다중: <code>(1 3 5)</code>\n"
        "  전체: <code>전체</code>\n"
        "  건너뜀: <code>/skip</code>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ 자동 스캔 주기: {POLL_INTERVAL_SEC // 3600}시간"
    )
    send_telegram(msg)


# ════════════════════════════════════════════════════════════════
#  명령어: 파일 목록
# ════════════════════════════════════════════════════════════════

def send_file_list(processed: set):
    try:
        exists = WATCH_PATH.exists()
    except OSError as e:
        send_telegram(f"⚠️ 드라이브 접근 오류\n📂 {WATCH_PATH}\n{e}")
        return
    if not exists:
        send_telegram(
            f"⚠️ 네트워크 드라이브 연결 끊김\n📂 {WATCH_PATH}"
        )
        return

    try:
        all_mkv = sorted(
            WATCH_PATH.glob("*.mkv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
    except OSError as e:
        send_telegram(f"⚠️ 드라이브 스캔 오류\n{e}")
        return

    new_files = [p for p in all_mkv if str(p) not in processed]
    done_files = [p for p in all_mkv if str(p) in processed]

    if not all_mkv:
        send_telegram("📂 감시 경로에 .mkv 파일이 없습니다")
        return

    msg = f"📂 <b>영상 파일 현황</b>\n"
    msg += f"━━━━━━━━━━━━━━━━━━━\n"

    if new_files:
        msg += f"🆕 <b>미처리 {len(new_files)}개</b>\n"
        for i, p in enumerate(new_files[:10], 1):
            size_gb = p.stat().st_size / (1024 ** 3)
            mtime = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m/%d %H:%M")
            msg += f"  {i}. {p.name}\n"
            msg += f"     📅 {mtime}  💾 {size_gb:.1f}GB\n"
        if len(new_files) > 10:
            msg += f"  … 외 {len(new_files) - 10}개\n"
    else:
        msg += "✅ 미처리 파일 없음\n"

    msg += f"\n✅ 완료: {len(done_files)}개\n"
    msg += "━━━━━━━━━━━━━━━━━━━\n"
    msg += "처리하려면 <code>/edit</code> 또는 <code>/scan</code>"
    send_telegram(msg)


# ════════════════════════════════════════════════════════════════
#  네트워크 경로 감시
# ════════════════════════════════════════════════════════════════

def check_mount() -> bool:
    try:
        exists = WATCH_PATH.exists()
    except OSError as e:
        log.warning(f"  드라이브 접근 오류 (Errno {e.errno}): {e}")
        send_telegram(
            f"⚠️ 네트워크 드라이브 접근 오류\n"
            f"📂 {WATCH_PATH}\n"
            f"Finder에서 다시 연결해주세요"
        )
        return False
    if not exists:
        log.warning(f"  경로 없음: {WATCH_PATH}")
        send_telegram(
            f"⚠️ 네트워크 드라이브 연결 끊김\n"
            f"📂 {WATCH_PATH}\n"
            f"Finder에서 다시 연결해주세요"
        )
        return False
    return True

def scan_new_files(processed: set) -> list:
    if not check_mount():
        return []

    try:
        all_mkv = sorted(
            WATCH_PATH.glob("*.mkv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
    except OSError as e:
        log.warning(f"  드라이브 스캔 오류: {e}")
        send_telegram(f"⚠️ 드라이브 스캔 중 오류 발생\n{e}")
        return []

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
    msg  = f"🎬 <b>새 영상 {len(new_files)}개 발견!</b>\n"
    msg += f"<i>(최신 순 정렬)</i>\n\n"
    for idx, f in enumerate(new_files, 1):
        msg += f"<b>{idx}.</b> {f['name']}\n"
        msg += f"     📅 {f['mtime']}  💾 {f['size_gb']:.1f}GB\n\n"
    msg += "━━━━━━━━━━━━━━━━━━━\n"
    msg += "📌 <b>처리할 번호를 입력해주세요</b>\n\n"
    msg += "  단일 선택: <code>2</code>\n"
    msg += "  다중 선택: <code>(1 3 5)</code>\n"
    msg += "  전체 선택: <code>전체</code>\n"
    msg += "  건너뛰기: <code>/skip</code>"

    send_telegram(msg)
    log.info("  텔레그램 목록 전송 완료 — 답장 대기 중...")

    base_update_id = get_last_update_id()
    elapsed = 0

    while elapsed < SELECTION_TIMEOUT:
        time.sleep(TELEGRAM_POLL_SEC)
        elapsed += TELEGRAM_POLL_SEC

        text, new_update_id = get_latest_reply(base_update_id)

        if text is None or new_update_id == base_update_id:
            if elapsed == 120:
                send_telegram(
                    f"⏳ 아직 선택 대기 중...\n"
                    f"번호 또는 '전체' / '/skip'으로 답장해주세요\n"
                    f"({(SELECTION_TIMEOUT - elapsed) // 60}분 후 자동 종료)"
                )
            continue

        base_update_id = new_update_id
        cmd = normalize_cmd(text)
        log.info(f"  답장 수신: '{text}' → '{cmd}'")

        if cmd == "skip":
            send_telegram("⏭ 스킵했습니다.\n다음 스캔 때 다시 알림드릴게요.")
            return []

        if cmd in ("all", "전체"):
            send_telegram(f"✅ 전체 {len(new_files)}개 선택\n처리를 시작합니다!")
            return new_files

        # ── 번호 파싱 ─────────────────────────────────────────
        stripped = text.strip()

        # 다중 선택: (1 3 5) 괄호 형식
        if stripped.startswith("(") and stripped.endswith(")"):
            inner = stripped[1:-1].replace(",", " ")
            parts = inner.split()
        # 단일 선택: 숫자 하나
        elif stripped.isdigit():
            parts = [stripped]
        else:
            send_telegram(
                "⚠️ 형식이 맞지 않습니다\n\n"
                "  단일: <code>2</code>\n"
                "  다중: <code>(1 3 5)</code>\n"
                "  전체: <code>전체</code>\n"
                "  건너뜀: <code>/skip</code>"
            )
            continue

        try:
            nums = [int(x) for x in parts if x.isdigit()]
        except ValueError:
            nums = []

        if not nums:
            send_telegram("⚠️ 올바른 번호를 입력해주세요")
            continue

        selected, invalid = [], []
        for n in nums:
            if 1 <= n <= len(new_files):
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
#  OpenClaw 실행 (비차단)
# ════════════════════════════════════════════════════════════════

def run_openclaw(video_path: Path) -> bool:
    script = BASE_DIR / "openclaw.py"
    if not script.exists():
        log.error(f"openclaw.py 없음: {script}")
        send_telegram(f"❌ openclaw.py 없음\n{script}")
        return False

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

    global _stop_requested
    _stop_requested = False

    process = subprocess.Popen(
        [sys.executable, str(script), str(video_path)],
        cwd=str(BASE_DIR)
    )
    poll_update_id = get_last_update_id()

    while process.poll() is None:
        time.sleep(5)
        text, poll_update_id = get_latest_reply(poll_update_id)
        if text:
            cmd = normalize_cmd(text)
            if cmd == "stop":
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                _stop_requested = True
                send_telegram(
                    f"🛑 처리 중단됨\n"
                    f"📁 {_batch['current_file']}\n"
                    f"완료: {_batch['done']}/{_batch['total']}개"
                )
                log.info(f"  [/stop] 처리 중단: {_batch['current_file']}")
                break
            elif cmd == "progress":
                send_progress_status()
            elif cmd == "status":
                mounted = WATCH_PATH.exists()
                send_telegram(
                    f"📡 OpenClaw 상태\n"
                    f"{'✅' if mounted else '❌'} 네트워크 드라이브: "
                    f"{'연결됨' if mounted else '연결 끊김'}\n"
                    f"🔄 현재 처리 중: {_batch['current_file']}"
                )
            elif cmd == "queue":
                done  = _batch["done"]
                total = _batch["total"]
                remain = total - done
                send_telegram(
                    f"📋 처리 대기열\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"✅ 완료: {done}개\n"
                    f"⏳ 남은: {remain}개\n"
                    f"🔄 현재: {_batch['current_file'] or '없음'}"
                )
            elif cmd == "help":
                send_help()

    ok = process.returncode == 0

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

    selected = wait_for_selection(new_files)

    if not selected:
        log.info("  선택 없음 — 건너뜀")
        return processed

    _batch["total"]      = len(selected)
    _batch["done"]       = 0
    _batch["done_files"] = []

    global _stop_requested
    for fi in selected:
        if _stop_requested:
            log.info("  [중단] 남은 파일 처리 건너뜀")
            send_telegram("⏭ 남은 파일 처리를 건너뜁니다")
            break

        video_path = fi["path_obj"]
        log.info(f"\n  처리 시작: {fi['name']}")
        try:
            run_openclaw(video_path)
        except Exception as e:
            log.error(f"  ✗ 실패: {fi['name']} — {e}")
            send_telegram(f"❌ 처리 실패\n📁 {fi['name']}\n오류: {e}")
            continue

        if not _stop_requested:
            processed.add(fi["path"])
            save_processed(processed)
            log.info(f"  ✓ 완료 기록: {fi['name']}")

    return processed


# ════════════════════════════════════════════════════════════════
#  편집 실행 — 최신 파일 즉시 처리
# ════════════════════════════════════════════════════════════════

def run_latest_file():
    try:
        exists = WATCH_PATH.exists()
    except OSError as e:
        send_telegram(
            f"⚠️ 드라이브 접근 오류\n"
            f"📂 {WATCH_PATH}\n"
            f"Finder에서 다시 연결해주세요"
        )
        return
    if not exists:
        send_telegram(
            f"⚠️ 네트워크 드라이브 연결 끊김\n"
            f"📂 {WATCH_PATH}\n"
            f"Finder에서 다시 연결해주세요"
        )
        return

    try:
        all_mkv = sorted(
            WATCH_PATH.glob("*.mkv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
    except OSError as e:
        send_telegram(f"⚠️ 드라이브 스캔 오류\n{e}")
        return

    if not all_mkv:
        send_telegram("⚠️ 감시 경로에 .mkv 파일이 없습니다")
        return

    latest  = all_mkv[0]
    size_gb = latest.stat().st_size / (1024 ** 3)
    mtime   = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%m/%d %H:%M")

    send_telegram(
        f"▶️ 편집 실행 — 최신 파일 처리 시작\n"
        f"📁 {latest.name}\n"
        f"📅 {mtime}  💾 {size_gb:.1f}GB"
    )
    log.info(f"  [/edit] 최신 파일 처리: {latest.name}")

    _batch["total"]      = 1
    _batch["done"]       = 0
    _batch["done_files"] = []

    try:
        run_openclaw(latest)
    except Exception as e:
        log.error(f"  ✗ 편집 실행 실패: {e}")
        send_telegram(f"❌ 편집 실행 실패\n오류: {e}")


# ════════════════════════════════════════════════════════════════
#  명령어 처리 (메인 루프에서 호출)
# ════════════════════════════════════════════════════════════════

def handle_command(cmd: str, processed: set, last_scan_time: list) -> set:
    """
    정규화된 명령어 처리.
    last_scan_time: [float] 리스트 (mutable reference)
    """
    if cmd == "help":
        send_help()

    elif cmd == "status":
        mounted = WATCH_PATH.exists()
        processed_now = load_processed()
        send_telegram(
            f"📡 <b>OpenClaw 상태</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{'✅' if mounted else '❌'} 네트워크 드라이브: "
            f"{'연결됨' if mounted else '연결 끊김'}\n"
            f"📝 누적 처리 파일: {len(processed_now)}개\n"
            f"⏱ 다음 자동 스캔: "
            f"{max(0, int((last_scan_time[0] + POLL_INTERVAL_SEC - time.time()) / 60))}분 후"
        )

    elif cmd == "progress":
        send_progress_status()

    elif cmd == "edit":
        run_latest_file()

    elif cmd == "list":
        send_file_list(processed)

    elif cmd == "scan":
        log.info("  [/scan] 즉시 스캔 시작")
        send_telegram("🔍 즉시 스캔을 시작합니다...")
        processed = scan_once(processed)
        last_scan_time[0] = time.time()
        save_processed(processed)

    elif cmd == "reset":
        if PROCESSED_LOG.exists():
            PROCESSED_LOG.unlink()
        processed = set()
        send_telegram(
            "🔄 처리 목록을 초기화했습니다.\n"
            "다음 스캔 시 모든 파일을 새 파일로 감지합니다."
        )
        log.info("  [/reset] 처리 목록 초기화")

    elif cmd == "stop":
        if _batch["current_file"]:
            send_telegram(
                "⚠️ 처리 중인 작업이 있습니다\n"
                f"🔄 {_batch['current_file']}\n"
                "분석 중에는 /stop이 자동 적용됩니다"
            )
        else:
            send_telegram("ℹ️ 현재 처리 중인 작업이 없습니다")

    elif cmd == "queue":
        done  = _batch["done"]
        total = _batch["total"]
        if total == 0:
            send_telegram("ℹ️ 현재 처리 대기열이 없습니다")
        else:
            remain = total - done
            done_list = "\n".join(f"  ✓ {n}" for n in _batch["done_files"]) or "  없음"
            send_telegram(
                f"📋 <b>처리 대기열</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"✅ 완료: {done}개\n"
                f"⏳ 남은: {remain}개\n"
                f"🔄 현재: {_batch['current_file'] or '없음'}\n"
                f"📝 완료 목록:\n{done_list}"
            )

    return processed


# ════════════════════════════════════════════════════════════════
#  메인
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="OpenClaw Watcher v3.3")
    parser.add_argument("--once",  action="store_true", help="1회 스캔 후 종료")
    parser.add_argument("--poll",  type=int, default=POLL_INTERVAL_SEC,
                        help=f"폴링 간격(초) [기본: {POLL_INTERVAL_SEC}]")
    parser.add_argument("--reset", action="store_true",
                        help="처리 완료 목록 초기화")
    args = parser.parse_args()

    log.info("═" * 48)
    log.info("  OpenClaw Watcher v3.3 시작")
    log.info(f"  감시 경로  : {WATCH_PATH}")
    log.info(f"  결과물 경로: {OUTPUT_DIR}")
    log.info(f"  폴링 간격  : {args.poll}초 ({args.poll // 3600}시간 {(args.poll % 3600) // 60}분)")
    log.info("═" * 48)

    if args.reset:
        if PROCESSED_LOG.exists():
            PROCESSED_LOG.unlink()
        log.info("  처리 완료 목록 초기화됨\n")

    if not args.once and not WATCH_PATH.exists():
        log.warning(f"  감시 경로 없음 (마운트 시 자동 재시도): {WATCH_PATH}")

    processed = load_processed()
    log.info(f"  기처리 파일: {len(processed)}개\n")

    send_telegram(
        f"🦞 <b>OpenClaw Watcher v3.3 시작!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📂 감시: {WATCH_PATH.name}\n"
        f"⏱ 스캔 주기: {args.poll // 3600}시간\n"
        f"💬 /help 으로 명령어 목록 확인"
    )

    # ── 1회 실행 모드 ─────────────────────────────────────────
    if args.once:
        scan_once(processed)
        log.info("  [--once] 완료")
        return

    # ── 상시 실행 모드 ────────────────────────────────────────
    # 텔레그램 update_id 초기화 (기존 메시지 무시)
    last_update_id = get_last_update_id()

    # last_scan_time[0] = 0 → 시작 직후 즉시 1회 스캔
    last_scan_time = [0.0]

    try:
        while True:
            # ── 텔레그램 명령 체크 (CMD_POLL_SEC마다) ───────────
            text, last_update_id = get_latest_reply(last_update_id)
            if text:
                cmd = normalize_cmd(text)
                log.info(f"  [텔레그램 명령] '{text}' → '{cmd}'")
                processed = handle_command(cmd, processed, last_scan_time)

            # ── 정기 파일 스캔 (POLL_INTERVAL_SEC마다) ──────────
            now = time.time()
            if now - last_scan_time[0] >= args.poll:
                processed = scan_once(processed)
                last_scan_time[0] = time.time()
                next_min = args.poll // 60
                log.info(f"  다음 스캔까지 {next_min}분 대기...\n")

            time.sleep(CMD_POLL_SEC)

    except KeyboardInterrupt:
        log.info("\n  watcher 종료")
        send_telegram("🛑 OpenClaw Watcher 종료")


if __name__ == "__main__":
    main()
