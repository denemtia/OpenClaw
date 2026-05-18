"""
notify.py — 텔레그램 알림 모듈
────────────────────────────────
.env 설정:
  TELEGRAM_TOKEN=your_bot_token
  TELEGRAM_CHAT_ID=your_chat_id
"""

import os
import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

TOKEN   = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def send_telegram(message: str) -> bool:
    if not TOKEN or not CHAT_ID:
        print("[notify] TELEGRAM_TOKEN / CHAT_ID 미설정")
        return False
    try:
        url  = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        resp = requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        print(f"[notify] 전송 실패: {e}")
        return False


def send_photo(image_path: str, caption: str = "") -> bool:
    """썸네일 이미지 전송"""
    if not TOKEN or not CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        with open(image_path, "rb") as f:
            resp = requests.post(url, data={
                "chat_id": CHAT_ID,
                "caption": caption
            }, files={"photo": f}, timeout=30)
        return resp.status_code == 200
    except Exception as e:
        print(f"[notify] 사진 전송 실패: {e}")
        return False
