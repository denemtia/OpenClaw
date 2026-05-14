#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")
API_BASE  = f"https://api.telegram.org/bot{BOT_TOKEN}"

def _send(payload):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(f"{API_BASE}/sendMessage", json=payload, timeout=10)
        return r.ok
    except Exception as e:
        print(f"[NOTIFY] 전송 실패: {e}")
        return False

def send(text, parse_mode="HTML"):
    return _send({"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode})

def send_file(file_path, caption=""):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        suffix = Path(file_path).suffix.lower()
        if suffix in [".jpg", ".jpeg", ".png"]:
            method, key = "sendPhoto", "photo"
        elif suffix == ".mp4":
            method, key = "sendVideo", "video"
        else:
            method, key = "sendDocument", "document"
        with open(file_path, "rb") as f:
            r = requests.post(f"{API_BASE}/{method}",
                data={"chat_id": CHAT_ID, "caption": caption},
                files={key: f}, timeout=120)
        return r.ok
    except Exception as e:
        print(f"[NOTIFY] 파일 전송 실패: {e}")
        return False

def notify_start(filename):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    send(f"🎬 <b>OpenClaw 시작</b>\n📁 {filename}\n🕐 {now}")

def notify_split_done(chunk_count):
    send(f"✂️ <b>분할 완료</b>\n📦 {chunk_count}개 chunk 생성")

def notify_event_detected(count, abs_sec):
    mins, secs = divmod(int(abs_sec), 60)
    send(f"🎯 <b>탈락 감지!</b>\n⏱ {mins}분 {secs}초\n📊 누적 {count}건")

def notify_clip_created(clip_name, total):
    send(f"🎞 <b>클립 생성</b>\n📄 {clip_name}\n📊 총 {total}개")

def notify_merge_done(output_path, elapsed_sec):
    mins, secs = divmod(int(elapsed_sec), 60)
    size_mb = Path(output_path).stat().st_size / 1024 / 1024
    send(f"✅ <b>OpenClaw 완료!</b>\n📁 {Path(output_path).name}\n💾 {size_mb:.1f} MB\n⏱ {mins}분 {secs}초")

def notify_upload_done(drive_url):
    send(f"☁️ <b>Drive 업로드 완료</b>\n🔗 {drive_url}")

def notify_error(step, message):
    send(f"❌ <b>오류 발생</b>\n📍 {step}\n💬 {message}")

def notify_progress(step, detail=""):
    send(f"⏳ <b>{step}</b>" + (f"\n{detail}" if detail else ""))
