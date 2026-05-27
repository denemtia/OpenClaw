"""
watcher.py — OpenClaw 로컬 네트워크 감시 데몬 v4.0
─────────────────────────────────────────────────────────────────
변경사항 (v3.3 → v4.0):
  - 폴링 방식(1시간) → watchdog 파일시스템 즉시 감지로 변경
    새 .mkv 파일 추가 시 60초 이내 자동 감지 및 알림
  - 드라이브 재연결 시 watchdog 자동 재시작
  - 코드 구조 정리 및 가독성 개선

사용법:
  python watcher.py            # 상시 실행 (즉시 감지)
  python watcher.py --once     # 1회 스캔 후 종료
  python watcher.py --reset    # 처리 목록 초기화 후 시작
"""

import os
import sys
import time
import json
import queue
import logging
import argparse
import subprocess
import threading
from pathlib import Path
from datetime import datetime

try:
    import requests
except ImportError:
    print("[ERROR] pip install requests")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv(Path.home() / "Desktop" / "OpenClaw-project" / ".env")
except ImportError:
    pass

try:
    from watchdog.observers.polling import PollingObserver
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False


# ════════════════════════════════════════════════════════════════
#  설정
# ════════════════════════════════════════════════════════════════

BASE_DIR      = Path.home() / "Desktop" / "OpenClaw-project"
OUTPUT_DIR    = BASE_DIR / "output"
PROCESSED_LOG = BASE_DIR / ".processed_files.json"
PROGRESS_FILE = BASE_DIR / ".progress.json"
WATCH_PATH    = Path("/Volumes/영상 녹화2")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_TOKEN", ""))
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SCAN_INTERVAL_SEC  = 60    # watchdog 폴링 체크 주기 (초) — 새 파일 감지 속도
CMD_POLL_SEC       = 5     # 텔레그램 명령 체크 주기 (초)
TELEGRAM_POLL_SEC  = 3     # 선택 대기 중 답장 확인 주기 (초)
SELECTION_TIMEOUT  = 300   # 파일 선택 대기 시간 (초, 5분)
RECONNECT_INTERVAL = 30    # 드라이브 재연결 감지 주기 (초)

# ── 전역 상태 ─────────────────────────────────────────────────
_batch: dict = {
    "total": 0,
    "done": 0,
    "current_file": None,
    "current_start": None,
    "done_files": [],
}
_stop_requested  = False
_file_queue: queue.Queue = queue.Queue()


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
        logging.FileHandler(BASE_DIR / "watcher.log", encoding="utf-8"),
    ],
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
        json.dump(
            {"processed_paths": list(processed), "updated_at": datetime.now().isoformat()},
            f, ensure_ascii=False, indent=2,
        )


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
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        return resp.status_code == 200
    except Exception as e:
        log.error(f"텔레그램 전송 실패: {e}")
        return False

def get_last_update_id() -> int:
    try:
        resp    = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"limit": 1}, timeout=10,
        )
        updates = resp.json().get("result", [])
        if updates:
            return updates[-1]["update_id"]
    except Exception:
        pass
    return 0

def get_latest_reply(after_update_id: int) -> tuple:
    try:
        resp    = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": after_update_id + 1, "timeout": 2},
            timeout=10,
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
# ════════════════════════════════════════════════════════════════

def normalize_cmd(text: str) -> str:
    t = text.strip().lstrip("/").lower()
    aliases = {
        "도움말": "help",   "명령어": "help",
        "상태": "status",   "상태확인": "status",
        "진행상황": "progress", "진행": "progress",
        "편집실행": "edit", "편집 실행": "edit",
        "파일목록": "list", "목록": "list", "새파일": "list",
        "스캔": "scan",     "즉시스캔": "scan",
        "초기화": "reset",
        "스킵": "skip",     "건너뛰기": "skip",
        "전체": "all",
        "중단": "stop",     "취소": "stop", "cancel": "stop",
        "처리중단": "stop", "처리취소": "stop",
        "대기열": "queue",  "대기": "queue",
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
        "  /scan       — 즉시 폴더 스캔\n"
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
        "⚡ 감지 방식: 즉시 감지 (파일 추가 시 자동 알림)"
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
        send_telegram(f"⚠️ 네트워크 드라이브 연결 끊김\n📂 {WATCH_PATH}")
        return

    try:
        all_mkv = sorted(WATCH_PATH.glob("*.mkv"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        send_telegram(f"⚠️ 드라이브 스캔 오류\n{e}")
        return

    new_files  = [p for p in all_mkv if str(p) not in processed]
    done_files = [p for p in all_mkv if str(p) in processed]

    if not all_mkv:
        send_telegram("📂 감시 경로에 .mkv 파일이 없습니다")
        return

    msg = "📂 <b>영상 파일 현황</b>\n━━━━━━━━━━━━━━━━━━━\n"
    if new_files:
        msg += f"🆕 <b>미처리 {len(new_files)}개</b>\n"
        for i, p in enumerate(new_files[:10], 1):
            size_gb = p.stat().st_size / (1024 ** 3)
            mtime   = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m/%d %H:%M")
            msg += f"  {i}. {p.name}\n     📅 {mtime}  💾 {size_gb:.1f}GB\n"
        if len(new_files) > 10:
            msg += f"  … 외 {len(new_files) - 10}개\n"
    else:
        msg += "✅ 미처리 파일 없음\n"

    msg += f"\n✅ 완료: {len(done_files)}개\n"
    msg += "━━━━━━━━━━━━━━━━━━━\n"
    msg += "처리하려면 <code>/edit</code> 또는 <code>/scan</code>"
    send_telegram(msg)


# ════════════════════════════════════════════════════════════════
#  드라이브 확인 및 스캔
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
        return False
    return True

def scan_new_files(processed: set) -> list:
    """드라이브 스캔 후 미처리 .mkv 목록 반환."""
    if not check_mount():
        return []
    try:
        all_mkv = sorted(WATCH_PATH.glob("*.mkv"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        log.warning(f"  드라이브 스캔 오류: {e}")
        send_telegram(f"⚠️ 드라이브 스캔 중 오류 발생\n{e}")
        return []

    return [
        {
            "path":     str(p),
            "name":     p.name,
            "size_gb":  p.stat().st_size / (1024 ** 3),
            "mtime":    datetime.fromtimestamp(p.stat().st_mtime).strftime("%m/%d %H:%M"),
            "path_obj": p,
        }
        for p in all_mkv if str(p) not in processed
    ]


# ════════════════════════════════════════════════════════════════
#  watchdog 핸들러 & Observer 관리
# ════════════════════════════════════════════════════════════════

if HAS_WATCHDOG:
    class MkvEventHandler(FileSystemEventHandler):
        """새 .mkv 파일 생성/이동 감지 → 큐에 추가."""
        def __init__(self, file_queue: queue.Queue):
            self.q = file_queue

        def _enqueue(self, path: str):
            if path.endswith(".mkv"):
                self.q.put(path)
                log.info(f"  [watchdog] 새 파일 감지: {Path(path).name}")

        def on_created(self, event):
            if not event.is_directory:
                self._enqueue(event.src_path)

        def on_moved(self, event):
            if not event.is_directory:
                self._enqueue(event.dest_path)

def start_observer() -> object:
    """watchdog PollingObserver 시작. 실패 시 None 반환."""
    if not HAS_WATCHDOG:
        return None
    try:
        if not WATCH_PATH.exists():
            return None
        observer = PollingObserver(timeout=SCAN_INTERVAL_SEC)
        observer.schedule(MkvEventHandler(_file_queue), str(WATCH_PATH), recursive=False)
        observer.start()
        log.info(f"  [watchdog] 감시 시작: {WATCH_PATH}  (체크 주기: {SCAN_INTERVAL_SEC}초)")
        return observer
    except Exception as e:
        log.warning(f"  [watchdog] 시작 실패: {e}")
        return None

def stop_observer(observer):
    """watchdog observer 안전 중지."""
    if observer and observer.is_alive():
        try:
            observer.stop()
            observer.join(timeout=3)
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
#  파일 선택 대화
# ════════════════════════════════════════════════════════════════

def wait_for_selection(new_files: list) -> list:
    msg  = f"🎬 <b>새 영상 {len(new_files)}개 발견!</b>\n<i>(최신 순 정렬)</i>\n\n"
    for idx, f in enumerate(new_files, 1):
        msg += f"<b>{idx}.</b> {f['name']}\n     📅 {f['mtime']}  💾 {f['size_gb']:.1f}GB\n\n"
    msg += (
        "━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>처리할 번호를 입력해주세요</b>\n\n"
        "  단일 선택: <code>2</code>\n"
        "  다중 선택: <code>(1 3 5)</code>\n"
        "  전체 선택: <code>전체</code>\n"
        "  건너뛰기: <code>/skip</code>"
    )
    send_telegram(msg)
    log.info("  텔레그램 목록 전송 완료 — 답장 대기 중...")

    base_update_id = get_last_update_id()
    elapsed        = 0

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
            send_telegram("⏭ 스킵했습니다.\n새 파일이 추가되면 다시 알림드릴게요.")
            return []

        if cmd in ("all", "전체"):
            send_telegram(f"✅ 전체 {len(new_files)}개 선택\n처리를 시작합니다!")
            return new_files

        # ── 번호 파싱 ─────────────────────────────────────────
        stripped = text.strip()
        if stripped.startswith("(") and stripped.endswith(")"):
            parts = stripped[1:-1].replace(",", " ").split()
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

        nums = [int(x) for x in parts if x.isdigit()]
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
            send_telegram(f"⚠️ 없는 번호: {invalid}\n1~{len(new_files)} 사이로 입력해주세요")
            continue

        if selected:
            names = "\n".join(f"  • {f['name']}" for f in selected)
            send_telegram(f"✅ {len(selected)}개 선택\n{names}\n\n처리를 시작합니다!")
            return selected

    log.warning(f"  {SELECTION_TIMEOUT}초 내 답장 없음 — 다음 감지까지 대기")
    send_telegram(
        f"⏰ {SELECTION_TIMEOUT // 60}분 내 선택이 없어 대기를 종료합니다\n"
        f"새 파일이 추가되면 다시 알림드릴게요"
    )
    return []


# ════════════════════════════════════════════════════════════════
#  진행상황 조회
# ════════════════════════════════════════════════════════════════

def send_progress_status():
    if _batch["total"] == 0:
        send_telegram("📊 현재 처리 중인 작업이 없습니다")
        return

    msg = "📊 <b>OpenClaw 진행상황</b>\n━━━━━━━━━━━━━━━━━━━\n"
    msg += f"✅ 완료: {_batch['done']}/{_batch['total']}개\n"

    if _batch["current_file"]:
        elapsed     = time.time() - _batch["current_start"]
        elapsed_str = f"{int(elapsed // 60)}분 {int(elapsed % 60)}초"
        msg += f"🔄 처리 중: <code>{_batch['current_file']}</code>\n"
        msg += f"⏱ 경과: {elapsed_str}\n"

        if PROGRESS_FILE.exists():
            try:
                prog  = json.load(open(PROGRESS_FILE, encoding="utf-8"))
                pct   = prog.get("progress_pct", 0)
                cur   = prog.get("current_hhmmss", "")
                total = prog.get("total_hhmmss", "")
                kills = prog.get("kill_count", 0)
                msg += f"📈 스캔: {pct:.0f}%  ({cur} / {total})\n"
                msg += f"✂️ 탈락 감지: {kills}개\n"
            except Exception:
                pass

    if _batch["done_files"]:
        show  = _batch["done_files"][-3:]
        more  = len(_batch["done_files"]) - len(show)
        msg  += "📝 완료 목록:\n"
        if more > 0:
            msg += f"  … 외 {more}개\n"
        for name in show:
            msg += f"  ✓ {name}\n"

    send_telegram(msg)


# ════════════════════════════════════════════════════════════════
#  OpenClaw 실행
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

    process        = subprocess.Popen(
        [sys.executable, str(script), str(video_path)],
        cwd=str(BASE_DIR),
    )
    poll_update_id = get_last_update_id()

    while process.poll() is None:
        time.sleep(5)
        text, poll_update_id = get_latest_reply(poll_update_id)
        if not text:
            continue
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
            try:
                mounted = WATCH_PATH.exists()
            except OSError:
                mounted = False
            send_telegram(
                f"📡 OpenClaw 상태\n"
                f"{'✅' if mounted else '❌'} 드라이브: {'연결됨' if mounted else '연결 끊김'}\n"
                f"🔄 처리 중: {_batch['current_file']}"
            )
        elif cmd == "queue":
            done   = _batch["done"]
            total  = _batch["total"]
            send_telegram(
                f"📋 처리 대기열\n━━━━━━━━━━━━━━━━━━━\n"
                f"✅ 완료: {done}개\n"
                f"⏳ 남은: {total - done}개\n"
                f"🔄 현재: {_batch['current_file'] or '없음'}"
            )
        elif cmd == "help":
            send_help()

    if _stop_requested:
        _batch["current_file"]  = None
        _batch["current_start"] = None
        return False

    ok = process.returncode == 0
    _batch["done"] += 1
    _batch["current_file"]  = None
    _batch["current_start"] = None

    result_dir = OUTPUT_DIR / f"{video_path.stem}_openclaw"
    if ok:
        clip_count = len(list(result_dir.glob("*.mp4"))) if result_dir.exists() else 0
        _batch["done_files"].append(video_path.name)
        send_telegram(
            f"✅ 분석 완료  ({_batch['done']}/{_batch['total']})\n"
            f"📁 {video_path.name}\n"
            f"✂️ 탈락 클립 {clip_count}개 추출\n"
            f"📂 결과물: {result_dir.name}/"
        )
    else:
        send_telegram(f"❌ 분석 실패\n📁 {video_path.name}")

    return ok


# ════════════════════════════════════════════════════════════════
#  배치 처리 (선택된 파일 순서대로 실행)
# ════════════════════════════════════════════════════════════════

def process_batch(files: list, processed: set) -> set:
    """파일 목록을 순서대로 처리하고 processed 집합 반환."""
    _batch["total"]      = len(files)
    _batch["done"]       = 0
    _batch["done_files"] = []

    global _stop_requested
    for fi in files:
        if _stop_requested:
            log.info("  [중단] 남은 파일 처리 건너뜀")
            send_telegram("⏭ 남은 파일 처리를 건너뜁니다")
            break
        log.info(f"\n  처리 시작: {fi['name']}")
        try:
            run_openclaw(fi["path_obj"])
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
#  1회 스캔 (scan_once)
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
        return processed

    return process_batch(selected, processed)


# ════════════════════════════════════════════════════════════════
#  편집 실행 (/edit — 최신 파일 즉시 처리)
# ════════════════════════════════════════════════════════════════

def run_latest_file():
    try:
        exists = WATCH_PATH.exists()
    except OSError:
        send_telegram(f"⚠️ 드라이브 접근 오류\n📂 {WATCH_PATH}\nFinder에서 다시 연결해주세요")
        return
    if not exists:
        send_telegram(f"⚠️ 네트워크 드라이브 연결 끊김\n📂 {WATCH_PATH}\nFinder에서 다시 연결해주세요")
        return

    try:
        all_mkv = sorted(WATCH_PATH.glob("*.mkv"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
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
#  명령어 처리
# ════════════════════════════════════════════════════════════════

def handle_command(cmd: str, processed: set, observer_holder: list) -> set:
    if cmd == "help":
        send_help()

    elif cmd == "status":
        try:
            mounted = WATCH_PATH.exists()
        except OSError:
            mounted = False
        watching      = (HAS_WATCHDOG and observer_holder[0] is not None
                         and observer_holder[0].is_alive())
        processed_now = load_processed()
        send_telegram(
            f"📡 <b>OpenClaw 상태</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{'✅' if mounted else '❌'} 드라이브: {'연결됨' if mounted else '연결 끊김'}\n"
            f"{'👁 즉시 감지 중' if watching else '⚠️ 감시 중단 (재연결 대기)'}\n"
            f"📝 누적 처리 파일: {len(processed_now)}개"
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
        save_processed(processed)

    elif cmd == "reset":
        if PROCESSED_LOG.exists():
            PROCESSED_LOG.unlink()
        processed = set()
        send_telegram("🔄 처리 목록을 초기화했습니다.\n다음 감지 시 모든 파일을 새 파일로 인식합니다.")
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
            done_list = "\n".join(f"  ✓ {n}" for n in _batch["done_files"]) or "  없음"
            send_telegram(
                f"📋 <b>처리 대기열</b>\n━━━━━━━━━━━━━━━━━━━\n"
                f"✅ 완료: {done}개\n"
                f"⏳ 남은: {total - done}개\n"
                f"🔄 현재: {_batch['current_file'] or '없음'}\n"
                f"📝 완료 목록:\n{done_list}"
            )

    return processed


# ════════════════════════════════════════════════════════════════
#  메인
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="OpenClaw Watcher v4.0")
    parser.add_argument("--once",  action="store_true", help="1회 스캔 후 종료")
    parser.add_argument("--reset", action="store_true", help="처리 완료 목록 초기화")
    args = parser.parse_args()

    detect_mode = f"watchdog 즉시감지 ({SCAN_INTERVAL_SEC}초 체크)" if HAS_WATCHDOG \
                  else f"직접 스캔 (watchdog 미설치)"

    log.info("═" * 50)
    log.info("  OpenClaw Watcher v4.0 시작")
    log.info(f"  감시 경로  : {WATCH_PATH}")
    log.info(f"  결과물 경로: {OUTPUT_DIR}")
    log.info(f"  감지 방식  : {detect_mode}")
    log.info("═" * 50)

    if args.reset:
        if PROCESSED_LOG.exists():
            PROCESSED_LOG.unlink()
        log.info("  처리 완료 목록 초기화됨\n")

    processed = load_processed()
    log.info(f"  기처리 파일: {len(processed)}개\n")

    send_telegram(
        f"🦞 <b>OpenClaw Watcher v4.0 시작!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📂 감시: {WATCH_PATH.name}\n"
        f"⚡ 감지: {detect_mode}\n"
        f"💬 /help 으로 명령어 목록 확인"
    )

    # ── 1회 실행 모드 ─────────────────────────────────────────
    if args.once:
        scan_once(processed)
        log.info("  [--once] 완료")
        return

    # ── 상시 실행 모드 ────────────────────────────────────────
    last_update_id  = get_last_update_id()
    last_reconnect  = 0.0
    observer_holder = [None]   # mutable: [observer | None]

    # watchdog 시작 (드라이브 연결 시)
    if HAS_WATCHDOG:
        observer_holder[0] = start_observer()
        if not observer_holder[0]:
            log.warning("  드라이브 미연결 — 연결 후 자동 감시 시작")

    # 시작 직후 1회 스캔 (미처리 파일 확인)
    processed = scan_once(processed)

    try:
        while True:
            # ── 텔레그램 명령 체크 ──────────────────────────────
            text, last_update_id = get_latest_reply(last_update_id)
            if text:
                cmd = normalize_cmd(text)
                log.info(f"  [명령] '{text}' → '{cmd}'")
                processed = handle_command(cmd, processed, observer_holder)

            # ── watchdog 감지 파일 처리 ────────────────────────
            if not _file_queue.empty():
                time.sleep(2)  # 잠시 대기 후 큐를 한꺼번에 수집
                paths = set()
                while not _file_queue.empty():
                    paths.add(_file_queue.get_nowait())

                proc_now  = load_processed()
                new_files = []
                for path_str in paths:
                    p = Path(path_str)
                    if path_str not in proc_now:
                        try:
                            new_files.append({
                                "path":     path_str,
                                "name":     p.name,
                                "size_gb":  p.stat().st_size / (1024 ** 3),
                                "mtime":    datetime.fromtimestamp(
                                    p.stat().st_mtime).strftime("%m/%d %H:%M"),
                                "path_obj": p,
                            })
                        except OSError:
                            pass

                if new_files:
                    new_files.sort(key=lambda x: x["mtime"], reverse=True)
                    log.info(f"  [watchdog] 새 파일 {len(new_files)}개 처리 시작")
                    selected  = wait_for_selection(new_files)
                    processed = process_batch(selected, processed) if selected else processed

            # ── watchdog 재시작 (드라이브 재연결 감지) ──────────
            if HAS_WATCHDOG:
                now = time.time()
                obs = observer_holder[0]
                if (obs is None or not obs.is_alive()) and \
                   now - last_reconnect >= RECONNECT_INTERVAL:
                    last_reconnect = now
                    try:
                        if WATCH_PATH.exists():
                            stop_observer(obs)
                            observer_holder[0] = start_observer()
                            if observer_holder[0]:
                                log.info("  [watchdog] 드라이브 재연결 — 감시 재시작")
                                send_telegram(
                                    f"✅ 드라이브 재연결 감지\n"
                                    f"📂 {WATCH_PATH.name} 감시 재시작\n"
                                    f"미처리 파일 확인 중..."
                                )
                                processed = scan_once(processed)
                    except OSError:
                        pass

            time.sleep(CMD_POLL_SEC)

    except KeyboardInterrupt:
        log.info("\n  watcher 종료")
        send_telegram("🛑 OpenClaw Watcher 종료")
        stop_observer(observer_holder[0])


if __name__ == "__main__":
    main()
