#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenClaw v3.0.0 - 자동 하이라이트 추출 파이프라인
"""
import os, sys, json, subprocess, shutil
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

try:
    import cv2
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False

try:
    import pytesseract
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

try:
    import notify as _notify
    def _n(fn, *a, **kw):
        try: fn(*a, **kw)
        except Exception: pass
except ImportError:
    class _notify:
        notify_start = notify_split_done = notify_event_detected = staticmethod(lambda *a,**kw: None)
        notify_clip_created = notify_merge_done = notify_upload_done = staticmethod(lambda *a,**kw: None)
        notify_error = notify_progress = staticmethod(lambda *a,**kw: None)
    def _n(fn, *a, **kw): pass

try:
    from uploader import upload as _drive_upload
    DRIVE_AVAILABLE = True
except ImportError:
    DRIVE_AVAILABLE = False
    def _drive_upload(*a, **kw): return None

SCRIPT_DIR  = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "config.yaml"
DEFAULT_CONFIG = {
    "pipeline":  {"chunk_duration_sec":3600,"frame_interval_sec":0.5,"skip_after_sec":60,"clip_offset_pre":30,"clip_total_len":60,"max_workers":4},
    "detection": {"keyword":"탈락","ocr_lang":"kor+eng","ocr_psm":6},
    "roi":       {"x_start":0.20,"x_end":0.80,"y_start":0.30,"y_end":0.70},
    "ocr_preprocess": {"upscale_factor":2,"denoise_h":10,"use_adaptive_threshold":True},
    "thumbnails":{"enabled":True,"width":320,"quality":90},
}

def load_config():
    if YAML_AVAILABLE and CONFIG_FILE.exists():
        with open(CONFIG_FILE,"r",encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        merged = {k: dict(v) for k,v in DEFAULT_CONFIG.items()}
        for s,v in loaded.items():
            if s in merged and isinstance(v,dict): merged[s].update(v)
            else: merged[s]=v
        return merged
    return {k: dict(v) for k,v in DEFAULT_CONFIG.items()}

CFG = load_config()
PC,DC,RC,OC,TC = CFG["pipeline"],CFG["detection"],CFG["roi"],CFG["ocr_preprocess"],CFG["thumbnails"]

BASE_DIR      = Path.home()/"Desktop"/"OpenClaw"
INPUT_FILE    = BASE_DIR/"input"/"vod_latest.mp4"
CHUNKS_DIR    = BASE_DIR/"input"/"chunks"
CLIPS_DIR     = BASE_DIR/"input"/"clips"
OUTPUT_DIR    = BASE_DIR/"output"
THUMBS_DIR    = OUTPUT_DIR/"thumbs"
PROGRESS_FILE = BASE_DIR/"progress.json"

def log(msg, level="INFO"):
    icons={"INFO":"✅","WARN":"⚠️ ","ERROR":"❌","STEP":"🔷","SKIP":"⏭️ "}
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {icons.get(level,'  ')} {msg}",flush=True)

def load_progress():
    if PROGRESS_FILE.exists():
        try:
            with open(PROGRESS_FILE,"r",encoding="utf-8") as f: return json.load(f)
        except: pass
    return {"completed_chunks":[],"events":[],"clips":[],"split_done":False}

def save_progress(p):
    try:
        with open(PROGRESS_FILE,"w",encoding="utf-8") as f: json.dump(p,f,ensure_ascii=False,indent=2)
    except Exception as e: log(f"진행 저장 실패: {e}","WARN")

def clear_progress():
    if PROGRESS_FILE.exists(): PROGRESS_FILE.unlink()

def ensure_dirs():
    for d in [BASE_DIR/"input",CHUNKS_DIR,CLIPS_DIR,OUTPUT_DIR,THUMBS_DIR]:
        os.makedirs(d,exist_ok=True)
    log(f"작업 폴더 확인: {BASE_DIR}")

def check_deps():
    for t,i in [("ffmpeg","brew install ffmpeg"),("ffprobe","brew install ffmpeg")]:
        if shutil.which(t) is None:
            log(f"{t} 없음 → {i}","ERROR"); sys.exit(1)
    log("의존성 확인 완료")

def get_video_duration(path):
    try:
        r=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
            "-of","default=noprint_wrappers=1:nokey=1",str(path)],
            capture_output=True,text=True,timeout=30)
        v=r.stdout.strip()
        return float(v) if v else float(PC["chunk_duration_sec"])
    except: return float(PC["chunk_duration_sec"])

def run_ffmpeg(cmd, label=""):
    try:
        r=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=7200)
        if r.returncode!=0: log(f"ffmpeg 오류 ({label}): {r.stderr[-300:]}","WARN"); return False
        return True
    except subprocess.TimeoutExpired: log(f"ffmpeg 타임아웃 ({label})","WARN"); return False
    except Exception as e: log(f"ffmpeg 예외 ({label}): {e}","ERROR"); return False

def preprocess_for_ocr(gray):
    img=gray.copy()
    h=int(OC.get("denoise_h",10))
    if h>0: img=cv2.fastNlMeansDenoising(img,h=h)
    s=float(OC.get("upscale_factor",2))
    if s>1: img=cv2.resize(img,None,fx=s,fy=s,interpolation=cv2.INTER_CUBIC)
    if OC.get("use_adaptive_threshold",True):
        img=cv2.adaptiveThreshold(img,255,cv2.ADAPTIVE_THRESH_GAUSSIAN_C,cv2.THRESH_BINARY,11,2)
    else:
        _,img=cv2.threshold(img,0,255,cv2.THRESH_BINARY+cv2.THRESH_OTSU)
    return img

def save_thumbnail(frame, idx, abs_sec):
    if not TC.get("enabled",True): return
    try:
        tw=int(TC.get("width",320)); fh,fw=frame.shape[:2]; th=int(fh*tw/fw)
        thumb=cv2.resize(frame,(tw,th))
        fname=THUMBS_DIR/f"thumb_{idx:03d}_t{int(abs_sec)}s.jpg"
        cv2.imwrite(str(fname),thumb,[cv2.IMWRITE_JPEG_QUALITY,int(TC.get("quality",90))])
        log(f"  📸 썸네일: {fname.name}")
    except Exception as e: log(f"  썸네일 실패: {e}","WARN")
