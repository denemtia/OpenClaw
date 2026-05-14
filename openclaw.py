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

def step1_split(progress):
    log("="*52,"STEP"); log("[Step 1] 무손실 분할","STEP"); log("="*52,"STEP")
    if not INPUT_FILE.exists():
        log(f"원본 영상 없음: {INPUT_FILE}","ERROR"); sys.exit(1)
    existing=sorted(CHUNKS_DIR.glob("chunk_*.mp4"))
    if existing and progress.get("split_done"):
        log(f"이전 분할 재사용: {len(existing)}개","SKIP"); return existing
    for f in CHUNKS_DIR.glob("chunk_*.mp4"): f.unlink()
    cmd=["ffmpeg","-y","-i",str(INPUT_FILE),"-c","copy","-f","segment",
         "-segment_time",str(PC["chunk_duration_sec"]),"-reset_timestamps","1",
         str(CHUNKS_DIR/"chunk_%03d.mp4")]
    if not run_ffmpeg(cmd,"segment split"):
        log("분할 실패 → 단일 chunk","WARN")
        shutil.copy(INPUT_FILE,CHUNKS_DIR/"chunk_000.mp4")
    chunks=sorted(CHUNKS_DIR.glob("chunk_*.mp4"))
    log(f"분할 완료: {len(chunks)}개")
    for c in chunks: log(f"  - {c.name} ({c.stat().st_size/1024/1024:.1f} MB)")
    progress["split_done"]=True; save_progress(progress)
    return chunks

def _analyze_single_chunk(chunk_path, chunk_offset_sec, already_done):
    events=[]
    if chunk_path.name in already_done:
        log(f"  [재개] 스킵: {chunk_path.name}","SKIP"); return events
    if not (CV2_AVAILABLE and TESSERACT_AVAILABLE):
        log(f"  OCR 없음 → {chunk_path.name} 건너뜀","WARN"); return events
    try:
        cap=cv2.VideoCapture(str(chunk_path))
        if not cap.isOpened(): log(f"  열기 실패: {chunk_path.name}","WARN"); return events
        fps=cap.get(cv2.CAP_PROP_FPS) or 30.0
        step_frames=max(1,int(fps*float(PC["frame_interval_sec"])))
        total_frames=int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        log(f"  📂 {chunk_path.name} | {fps:.0f}fps | {total_frames/fps:.0f}s | offset={chunk_offset_sec:.0f}s")
        skip_until_sec=-1.0; frame_idx=0
        while True:
            cap.set(cv2.CAP_PROP_POS_FRAMES,frame_idx)
            ret,frame=cap.read()
            if not ret: break
            current_sec=frame_idx/fps; absolute_sec=chunk_offset_sec+current_sec
            if current_sec<skip_until_sec: frame_idx+=step_frames; continue
            fh,fw=frame.shape[:2]
            x1,x2=int(fw*float(RC["x_start"])),int(fw*float(RC["x_end"]))
            y1,y2=int(fh*float(RC["y_start"])),int(fh*float(RC["y_end"]))
            roi=frame[y1:y2,x1:x2]
            gray=cv2.cvtColor(roi,cv2.COLOR_BGR2GRAY)
            processed=preprocess_for_ocr(gray)
            try:
                text=pytesseract.image_to_string(processed,lang=DC["ocr_lang"],
                     config=f"--psm {DC['ocr_psm']}")
            except:
                try: text=pytesseract.image_to_string(processed,config=f"--psm {DC['ocr_psm']}")
                except: text=""
            if DC["keyword"] in text:
                log(f"  🎯 탈락! local={current_sec:.1f}s abs={absolute_sec:.1f}s")
                save_thumbnail(frame,len(events),absolute_sec)
                events.append({"absolute_time":absolute_sec,"chunk_name":chunk_path.name,
                    "chunk_path":str(chunk_path),"local_time":current_sec,"chunk_offset":chunk_offset_sec})
                _n(_notify.notify_event_detected,len(events),absolute_sec)
                skip_until_sec=current_sec+float(PC["skip_after_sec"])
            frame_idx+=step_frames
        cap.release()
        log(f"  ✅ {chunk_path.name} 완료 | {len(events)}건")
    except Exception as e: log(f"  예외 ({chunk_path.name}): {e}","WARN")
    return events

def step2_analyze_and_clip(chunks, progress):
    log("="*52,"STEP"); log("[Step 2] ROI 분석 + 클립 생성","STEP"); log("="*52,"STEP")
    offsets=[]; acc=0.0
    for chunk in chunks: offsets.append(acc); acc+=get_video_duration(chunk)
    already_done=list(progress.get("completed_chunks",[]))
    all_events=list(progress.get("events",[]))
    max_w=min(int(PC["max_workers"]),len(chunks))
    log(f"병렬 분석: workers={max_w}")
    with ThreadPoolExecutor(max_workers=max_w) as pool:
        futures={pool.submit(_analyze_single_chunk,chunk,offsets[i],already_done):chunk
                 for i,chunk in enumerate(chunks)}
        for future in as_completed(futures):
            chunk=futures[future]
            try:
                evts=future.result(); all_events.extend(evts)
                already_done.append(chunk.name)
                progress["completed_chunks"]=already_done
                progress["events"]=all_events
                save_progress(progress)
            except Exception as e: log(f"예외 ({chunk.name}): {e}","WARN")
    all_events.sort(key=lambda e:e["absolute_time"])
    log(f"전체 이벤트: {len(all_events)}건")
    if not all_events: log("이벤트 없음.","WARN"); return []
    clips=[]; already_clips=list(progress.get("clips",[]))
    for idx,event in enumerate(all_events):
        clip_name=f"clip_{idx+1:03d}_t{int(event['absolute_time'])}s.mp4"
        clip_path=CLIPS_DIR/clip_name
        if clip_name in already_clips and clip_path.exists():
            clips.append(clip_path); continue
        abs_t=event["absolute_time"]; loc_t=event["local_time"]; success=False
        if INPUT_FILE.exists():
            start_t=max(0.0,abs_t-float(PC["clip_offset_pre"]))
            success=run_ffmpeg(["ffmpeg","-y","-ss",str(start_t),"-i",str(INPUT_FILE),
                "-t",str(PC["clip_total_len"]),"-c","copy",str(clip_path)],f"clip {idx+1}")
        if not success:
            cp=Path(event["chunk_path"])
            if cp.exists():
                ls=max(0.0,loc_t-float(PC["clip_offset_pre"]))
                success=run_ffmpeg(["ffmpeg","-y","-ss",str(ls),"-i",str(cp),
                    "-t",str(PC["clip_total_len"]),"-c","copy",str(clip_path)],f"clip {idx+1} fallback")
        if success and clip_path.exists():
            clips.append(clip_path); already_clips.append(clip_name)
            progress["clips"]=already_clips; save_progress(progress)
            log(f"  클립 저장: {clip_name}")
        else: log(f"  클립 실패: {clip_name}","WARN")
    log(f"클립 완료: {len(clips)}개")
    return clips

def step3_merge(clips, progress):
    log("="*52,"STEP"); log("[Step 3] 병합 → 최종 출력","STEP"); log("="*52,"STEP")
    if not clips: log("병합할 클립 없음.","WARN"); return None
    list_txt=CLIPS_DIR/"list.txt"
    with open(list_txt,"w",encoding="utf-8") as f:
        for clip in sorted(clips): f.write(f"file '{str(clip)}'\n")
    ts=datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file=OUTPUT_DIR/f"final_{ts}.mp4"
    if run_ffmpeg(["ffmpeg","-y","-f","concat","-safe","0","-i",str(list_txt),"-c","copy",str(output_file)],"merge") and output_file.exists():
        size_mb=output_file.stat().st_size/1024/1024
        log(f"최종 저장: {output_file.name} ({size_mb:.1f} MB)")
        clear_progress(); return output_file
    log("병합 실패","ERROR"); return None

def main():
    print()
    print("╔══════════════════════════════════════════╗")
    print("║   🦞  OpenClaw  v3.0.0                   ║")
    print("║   자동 하이라이트 추출 파이프라인         ║")
    print("╚══════════════════════════════════════════╝")
    print()
    start_time=datetime.now()
    ensure_dirs(); check_deps()
    progress=load_progress()
    if progress.get("completed_chunks"):
        done=len(progress["completed_chunks"]); evts=len(progress.get("events",[]))
        log(f"재개 모드 (chunk {done}개, 이벤트 {evts}건)")
        _n(_notify.notify_progress,"재개 모드",f"chunk {done}개, 이벤트 {evts}건")
    else:
        log("새 작업 시작"); _n(_notify.notify_start,INPUT_FILE.name)
    try:
        chunks=step1_split(progress)
        _n(_notify.notify_split_done,len(chunks))
        clips=step2_analyze_and_clip(chunks,progress)
        result=step3_merge(clips,progress)
    except SystemExit: raise
    except Exception as e:
        log(f"치명적 오류: {e}","ERROR")
        _n(_notify.notify_error,"파이프라인",str(e)); sys.exit(1)
    elapsed=(datetime.now()-start_time).total_seconds()
    mins,secs=divmod(int(elapsed),60)
    if result:
        _n(_notify.notify_merge_done,result,elapsed)
        if DRIVE_AVAILABLE:
            url=_drive_upload(result)
            if url: _n(_notify.notify_upload_done,url)
    print()
    print("╔══════════════════════════════════════════╗")
    if result:
        print("║   ✅  작업 완료                           ║")
        print(f"║   📁  {result.name:<36}║")
    else:
        print("║   ⚠️   작업 완료 (출력 없음)               ║")
    print(f"║   ⏱   {mins}분 {secs}초{' '*30}║")
    print("╚══════════════════════════════════════════╝")
    print()

if __name__ == "__main__":
    main()
