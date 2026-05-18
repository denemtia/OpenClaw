"""
uploader.py — Google Drive 업로드 모듈
────────────────────────────────────────
watcher.py가 내부적으로 업로드를 처리하지만,
openclaw.py 단독 실행 시 이 모듈을 통해 Drive 업로드 가능.

.env 설정:
  DRIVE_OUTPUT_FOLDER_ID=1DGkqk2hq1mTX-1TTQs6G5NhMFmSBR-W6
  GOOGLE_CREDENTIALS=credentials.json
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OUTPUT_FOLDER_ID = os.getenv("DRIVE_OUTPUT_FOLDER_ID", "")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")


def _get_service():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    scopes = ["https://www.googleapis.com/auth/drive"]
    creds  = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=scopes)
    return build("drive", "v3", credentials=creds)


def upload_to_drive(file_path: str, folder_id: str = None) -> str:
    """
    단일 파일을 Drive에 업로드.
    Returns: 업로드된 파일의 Drive ID
    """
    from googleapiclient.http import MediaFileUpload

    folder = folder_id or OUTPUT_FOLDER_ID
    if not folder:
        raise ValueError("DRIVE_OUTPUT_FOLDER_ID 미설정")

    service    = _get_service()
    path       = Path(file_path)
    mime_types = {
        ".mp4": "video/mp4",
        ".csv": "text/csv",
        ".json": "application/json",
        ".txt": "text/plain",
        ".jpg": "image/jpeg",
    }
    mime = mime_types.get(path.suffix.lower(), "application/octet-stream")

    media     = MediaFileUpload(file_path, mimetype=mime,
                                resumable=True, chunksize=32 * 1024 * 1024)
    file_meta = {"name": path.name, "parents": [folder]}
    result    = service.files().create(
        body=file_meta, media_body=media, fields="id"
    ).execute()

    print(f"[uploader] ✓ {path.name} → Drive (id: {result['id']})")
    return result["id"]
