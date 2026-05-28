"""
watcher.py — OpenClaw 텔레그램 봇 데몬 v5.0
─────────────────────────────────────────────────────────────────
변경사항 (v4.0 → v5.0):
  - 자동 파일 감지 완전 제거 (폴링 / watchdog 모두 제거)
  - 텔레그램 명령으로만 동작하는 순수 봇 방식
  - 코드 대폭 단순화

사용법:
  python watcher.py            # 봇 실행
  python watcher.py --reset    # 처리 목록 초기화 후 실행
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
    load_dotenv(Path.home() / "Desktop" / "OpenClaw-project" / ".env")
except ImportError:
    pass


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

CMD_POLL_SEC      = 5    # 텔레그램 명령 체크 주기 (초)
TELEGRAM_POLL_SEC = 3    # 파일 선택 대기 중 답장 확인 주기 (초)
SELECTION_TIMEOUT = 300  # 파일 선택 대기 시간 (초, 5분)

# ── 전역 상태 ─────────────────────────────────────────────────
_batch: dict = {
    "total": 0,
    "done": 0,
    "current_file": None,
    "current_start": None,
    "done_files": [],
}
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
            {"processed_paths": list(processed),
             "updated_at": datetime.now().isoformat()},
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
        resp = requests.get(
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
        resp = requests.get(
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
        "  /scan       — 폴더 스캔 후 처리할 파일 선택\n"
        "  /edit       — 최신 파일 즉시 처리\n\n"
        "📊 <b>상태 확인</b>\n"
        "  /status     — 드라이브 연결 + 처리 현황\n"
        "  /progress   — 현재 분석 진행률\n"
        "  /queue      — 처리 대기열 확인\n\n"
        "🛑 <b>제어</b>\n"
        "  /stop       — 현재 처리 작업 중단\n"
        "  /skip       — 파일 선택 건너뜀\n\n"
        "⚙️ <b>기타</b>\n"
        "  /help       — 이 도움말\n"
        "  /reset      — 처리 목록 초기화\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "📌 <b>파일 선택 방법</b>\n"
        "  단일: <code>2</code>\n"
        "  다중: <code>(1 3 5)</code>\n"
        "  전체: <code>전체</code>\n"
        "  건너뜀: <code>/skip</code>"
    )
    send_telegram(msg)


# ════════════════════════════════════════════════════════════════
#  드라이브 확인 및 파일 스캔
# ════════════════════════════════════════════════════════════════

def check_mount() -> bool:
    try:
        exists = WATCH_PATH.exists()
    except OSError as e:
        log.warning(f"  드라이브 접근 오류: {e}")
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

def get_new_files(processed: set) -> list:
    """드라이브에서 미처리 .mkv 파일 목록 반환."""
    if not check_mount():
        return []
    try:
        all_mkv = sorted(WATCH_PATH.glob("*.mkv"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        log.warning(f"  스캔 오류: {e}")
        send_telegram(f"⚠️ 드라이브 스캔 오류\n{e}")
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
#  명령어: 파일 목록 (/list)
# ════════════════════════════════════════════════════════════════

def send_file_list(processed: set):
    try:
        if not WATCH_PATH.exists():
            send_telegram(f"⚠️ 드라이브 연결 끊김\n📂 {WATCH_PATH}")
            return
        all_mkv    = sorted(WATCH_PATH.glob("*.mkv"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as e:
        send_telegram(f"⚠️ 드라이브 오류\n{e}")
        return

    new_files  = [p for p in all_mkv if str(p) not in processed]
    done_files = [p for p in all_mkv if str(p) in processed]

    if not all_mkv:
        send_telegram("📂 .mkv 파일이 없습니다")
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
    msg += "/scan 으로 처리 시작"
    send_telegram(msg)


# ════════════════════════════════════════════════════════════════
#  파일 선택 대화 (/scan)
# ════════════════════════════════════════════════════════════════

def wait_for_selection(new_files: list) -> list:
    msg  = f"🎬 <b>미처리 영상 {len(new_files)}개</b>\n<i>(최신 순 정렬)</i>\n\n"
    for idx, f in enumerate(new_files, 1):
        msg += f"<b>{idx}.</b> {f['name']}\n     📅 {f['mtime']}  💾 {f['size_gb']:.1f}GB\n\n"
    msg += (
        "━━━━━━━━━━━━━━━━━━━\n"
        "📌 처리할 번호를 입력해주세요\n\n"
        "  단일: <code>2</code>\n"
        "  다중: <code>(1 3 5)</code>\n"
        "  전체: <code>전체</code>\n"
        "  건너뜀: <code>/skip</code>"
    )
    send_telegram(msg)
    log.info("  파일 목록 전송 완료 — 답장 대기 중...")

    base_update_id = get_last_update_id()
    elapsed        = 0

    while elapsed < SELECTION_TIMEOUT:
        time.sleep(TELEGRAM_POLL_SEC)
        elapsed += TELEGRAM_POLL_SEC

        text, new_update_id = get_latest_reply(base_update_id)
        if text is None or new_update_id == base_update_id:
            if elapsed == 120:
                send_telegram(
                    f"⏳ 선택 대기 중...\n"
                    f"({(SELECTION_TIMEOUT - elapsed) // 60}분 후 자동 종료)"
                )
            continue

        base_update_id = new_update_id
        cmd = normalize_cmd(text)
        log.info(f"  답장: '{text}' → '{cmd}'")

        if cmd == "skip":
            send_telegram("⏭ 건너뜁니다.")
            return []

        if cmd in ("all", "전체"):
            send_telegram(f"✅ 전체 {len(new_files)}개 선택\n처리를 시작합니다!")
            return new_files

        # 번호 파싱
        stripped = text.strip()
        if stripped.startswith("(") and stripped.endswith(")"):
            parts = stripped[1:-1].replace(",", " ").split()
        elif stripped.isdigit():
            parts = [stripped]
        else:
            send_telegram(
                "⚠️ 형식 오류\n"
                "  단일: <code>2</code>\n"
                "  다중: <code>(1 3 5)</code>\n"
                "  전체: <code>전체</code> | 건너뜀: <code>/skip</code>"
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

    log.warning("선택 시간 초과")
    send_telegram(f"⏰ {SELECTION_TIMEOUT // 60}분 내 선택 없음 — 취소합니다\n/scan 으로 다시 시작하세요")
    return []


# ════════════════════════════════════════════════════════════════
#  진행상황 (/progress)
# ════════════════════════════════════════════════════════════════

def send_progress():
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
                msg += f"📈 스캔: {pct:.0f}%  ({cur}/{total})\n"
                msg += f"✂️ 탈락 감지: {kills}개\n"
            except Exception:
                pass

    if _batch["done_files"]:
        show = _batch["done_files"][-3:]
        more = len(_batch["done_files"]) - len(show)
        msg += "📝 완료:\n"
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
                f"🛑 처리 중단\n"
                f"📁 {_batch['current_file']}\n"
                f"완료: {_batch['done']}/{_batch['total']}개"
            )
            log.info(f"  [/stop] 중단: {_batch['current_file']}")
            break
        elif cmd == "progress":
            send_progress()
        elif cmd == "status":
            try:
                mounted = WATCH_PATH.exists()
            except OSError:
                mounted = False
            send_telegram(
                f"📡 드라이브: {'✅ 연결됨' if mounted else '❌ 끊김'}\n"
                f"🔄 처리 중: {_batch['current_file']}"
            )
        elif cmd == "queue":
            send_telegram(
                f"📋 대기열\n"
                f"✅ 완료: {_batch['done']}개\n"
                f"⏳ 남은: {_batch['total'] - _batch['done']}개\n"
                f"🔄 현재: {_batch['current_file']}"
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
            f"✂️ 클립 {clip_count}개 추출\n"
            f"📂 {result_dir.name}/"
        )
    else:
        send_telegram(f"❌ 분석 실패\n📁 {video_path.name}")

    return ok


# ════════════════════════════════════════════════════════════════
#  배치 처리
# ════════════════════════════════════════════════════════════════

def process_batch(files: list, processed: set) -> set:
    _batch["total"]      = len(files)
    _batch["done"]       = 0
    _batch["done_files"] = []

    global _stop_requested
    for fi in files:
        if _stop_requested:
            send_telegram("⏭ 남은 파일 처리를 건너뜁니다")
            break
        log.info(f"\n  처리 시작: {fi['name']}")
        try:
            run_openclaw(fi["path_obj"])
        except Exception as e:
            log.error(f"  ✗ {fi['name']} — {e}")
            send_telegram(f"❌ 처리 실패\n📁 {fi['name']}\n오류: {e}")
            continue
        if not _stop_requested:
            processed.add(fi["path"])
            save_processed(processed)

    return processed


# ════════════════════════════════════════════════════════════════
#  명령어 처리
# ════════════════════════════════════════════════════════════════

def handle_command(cmd: str, processed: set) -> set:

    if cmd == "help":
        send_help()

    elif cmd == "status":
        try:
            mounted = WATCH_PATH.exists()
        except OSError:
            mounted = False
        proc_now = load_processed()
        send_telegram(
            f"📡 <b>OpenClaw 상태</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{'✅' if mounted else '❌'} 드라이브: "
            f"{'연결됨' if mounted else '연결 끊김'}\n"
            f"📝 누적 처리: {len(proc_now)}개\n"
            f"💬 /scan 으로 처리 시작"
        )

    elif cmd == "progress":
        send_progress()

    elif cmd == "list":
        send_file_list(processed)

    elif cmd == "scan":
        log.info("  [/scan] 스캔 시작")
        new_files = get_new_files(processed)
        if not new_files:
            send_telegram("✅ 미처리 파일이 없습니다\n모든 파일이 처리되었습니다")
            return processed
        log.info(f"  미처리 {len(new_files)}개 발견")
        selected  = wait_for_selection(new_files)
        if selected:
            processed = process_batch(selected, processed)

    elif cmd == "edit":
        log.info("  [/edit] 최신 파일 처리")
        if not check_mount():
            return processed
        try:
            all_mkv = sorted(WATCH_PATH.glob("*.mkv"),
                             key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError as e:
            send_telegram(f"⚠️ 드라이브 오류\n{e}")
            return processed

        if not all_mkv:
            send_telegram("⚠️ .mkv 파일이 없습니다")
            return processed

        latest  = all_mkv[0]
        size_gb = latest.stat().st_size / (1024 ** 3)
        mtime   = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%m/%d %H:%M")
        send_telegram(
            f"▶️ 최신 파일 처리 시작\n"
            f"📁 {latest.name}\n"
            f"📅 {mtime}  💾 {size_gb:.1f}GB"
        )
        _batch["total"] = 1
        _batch["done"]  = 0
        _batch["done_files"] = []
        try:
            run_openclaw(latest)
        except Exception as e:
            send_telegram(f"❌ 처리 실패\n{e}")

    elif cmd == "reset":
        if PROCESSED_LOG.exists():
            PROCESSED_LOG.unlink()
        processed = set()
        send_telegram("🔄 처리 목록을 초기화했습니다\n/scan 으로 다시 시작하세요")
        log.info("  [/reset] 초기화")

    elif cmd == "stop":
        if _batch["current_file"]:
            send_telegram(
                f"⚠️ 처리 중: {_batch['current_file']}\n"
                "분석 중에 /stop을 보내면 자동 적용됩니다"
            )
        else:
            send_telegram("ℹ️ 현재 처리 중인 작업이 없습니다")

    elif cmd == "queue":
        if _batch["total"] == 0:
            send_telegram("ℹ️ 처리 대기열이 없습니다")
        else:
            done_list = "\n".join(f"  ✓ {n}" for n in _batch["done_files"]) or "  없음"
            send_telegram(
                f"📋 <b>처리 대기열</b>\n━━━━━━━━━━━━━━━━━━━\n"
                f"✅ 완료: {_batch['done']}개\n"
                f"⏳ 남은: {_batch['total'] - _batch['done']}개\n"
                f"🔄 현재: {_batch['current_file'] or '없음'}\n"
                f"📝 완료 목록:\n{done_list}"
            )

    return processed


# ════════════════════════════════════════════════════════════════
#  메인
# ════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="OpenClaw Watcher v5.0")
    parser.add_argument("--reset", action="store_true", help="처리 목록 초기화")
    args = parser.parse_args()

    log.info("═" * 50)
    log.info("  OpenClaw Watcher v5.0 시작")
    log.info(f"  감시 경로  : {WATCH_PATH}")
    log.info(f"  결과물 경로: {OUTPUT_DIR}")
    log.info(f"  동작 방식  : 텔레그램 명령 전용")
    log.info("═" * 50)

    if args.reset:
        if PROCESSED_LOG.exists():
            PROCESSED_LOG.unlink()
        log.info("  처리 목록 초기화됨")

    processed = load_processed()
    log.info(f"  기처리 파일: {len(processed)}개\n")

    send_telegram(
        f"🦞 <b>OpenClaw Watcher v5.0 시작!</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📂 감시: {WATCH_PATH.name}\n"
        f"💬 /scan — 파일 스캔 및 처리 시작\n"
        f"💬 /help — 전체 명령어 목록"
    )

    last_update_id = get_last_update_id()

    try:
        while True:
            text, last_update_id = get_latest_reply(last_update_id)
            if text:
                cmd = normalize_cmd(text)
                log.info(f"  [명령] '{text}' → '{cmd}'")
                processed = handle_command(cmd, processed)
            time.sleep(CMD_POLL_SEC)

    except KeyboardInterrupt:
        log.info("\n  watcher 종료")
        send_telegram("🛑 OpenClaw Watcher 종료")


if __name__ == "__main__":
    main()
