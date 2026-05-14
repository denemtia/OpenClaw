#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, time, shutil, subprocess
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

WATCH_DIR   = Path.home() / "Desktop" / "OpenClaw" / "input"
TARGET_FILE = WATCH_DIR / "vod_latest.mp4"
SCRIPT_DIR  = Path(__file__).parent
WIN_SHARE_PATH = os.getenv("WINDOWS_SHARE_PATH", "")
WIN_SHARE_USER = os.getenv("WINDOWS_SHARE_USER", "")
WIN_SHARE_PASS = os.getenv("WINDOWS_SHARE_PASS", "")
STABLE_CHECK_SEC   = 10
STABLE_CHECK_COUNT = 3
POLL_INTERVAL_SEC  = 30

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 👁  {msg}", flush=True)

def notify(text):
    try:
        from notify import send
        send(text)
    except Exception:
        pass

def is_file_stable(path):
    prev_size, stable = -1, 0
    while stable < STABLE_CHECK_COUNT:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == prev_size and size > 0:
            stable += 1
        else:
            stable = 0
        prev_size = size
        time.sleep(STABLE_CHECK_SEC)
    return True

def run_pipeline():
    log("🚀 OpenClaw 파이프라인 시작")
    notify("🚀 <b>OpenClaw 파이프라인 시작</b>")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "openclaw.py")],
            cwd=str(SCRIPT_DIR)
        )
        if result.returncode != 0:
            notify(f"⚠️ OpenClaw 비정상 종료 (code={result.returncode})")
    except Exception as e:
        notify(f"❌ 파이프라인 실행 실패: {e}")

def watch_loop():
    log(f"감시 시작: {WATCH_DIR}")
    notify("👁 <b>OpenClaw Watcher 시작</b>\n파일 도착 대기 중...")
    os.makedirs(WATCH_DIR, exist_ok=True)
    last_mtime = 0.0
    while True:
        try:
            if TARGET_FILE.exists():
                mtime = TARGET_FILE.stat().st_mtime
                if mtime != last_mtime:
                    log("새 파일 감지 — 복사 완료 대기 중...")
                    notify("📂 <b>새 파일 감지</b>\n복사 완료 확인 중...")
                    if is_file_stable(TARGET_FILE):
                        last_mtime = TARGET_FILE.stat().st_mtime
                        run_pipeline()
        except Exception as e:
            log(f"감시 루프 예외: {e}")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    watch_loop()
