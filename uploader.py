#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

FOLDER_ID        = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"

try:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google.oauth2.service_account import Credentials
    GDRIVE_AVAILABLE = True
except ImportError:
    GDRIVE_AVAILABLE = False

def _get_service():
    creds = Credentials.from_service_account_file(
        str(CREDENTIALS_FILE),
        scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds)

def _get_or_create_folder(service, name, parent_id):
    query = (f"name='{name}' and '{parent_id}' in parents and "
             f"mimeType='application/vnd.google-apps.folder' and trashed=false")
    results = service.files().list(q=query, fields="files(id)").execute()
    files   = results.get("files", [])
    if files:
        return files[0]["id"]
    folder = service.files().create(
        body={"name": name, "parents": [parent_id],
              "mimeType": "application/vnd.google-apps.folder"},
        fields="id"
    ).execute()
    return folder["id"]

def upload(file_path, folder_id=""):
    if not GDRIVE_AVAILABLE:
        print("[UPLOAD] google-api-python-client 미설치")
        return None
    if not CREDENTIALS_FILE.exists():
        print("[UPLOAD] credentials.json 없음")
        return None
    target_folder = folder_id or FOLDER_ID
    if not target_folder:
        print("[UPLOAD] GOOGLE_DRIVE_FOLDER_ID 미설정")
        return None
    try:
        service      = _get_service()
        date_str     = datetime.now().strftime("%Y-%m-%d")
        subfolder_id = _get_or_create_folder(service, date_str, target_folder)
        media    = MediaFileUpload(str(file_path), mimetype="video/mp4",
                                   resumable=True, chunksize=10*1024*1024)
        uploaded = service.files().create(
            body={"name": Path(file_path).name, "parents": [subfolder_id]},
            media_body=media, fields="id,webViewLink"
        ).execute()
        file_id  = uploaded.get("id")
        web_link = uploaded.get("webViewLink", "")
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"}
        ).execute()
        print(f"[UPLOAD] 완료: {web_link}")
        return web_link
    except Exception as e:
        print(f"[UPLOAD] 오류: {e}")
        return None
