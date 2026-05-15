# 🦞 OpenClaw

방송 영상에서 **"탈락" 자막을 자동 감지**하고 클립을 추출하는 자동화 파이프라인.

## 파이프라인 흐름

```
송출PC (172.30.1.22)
  └─ /영상 녹화2/*.mkv  (네트워크 공유)
         ↓ (30초마다 감시)
  watcher.py — 새 파일 감지
         ↓
  텔레그램 알림 — 파일 목록 전송
         ↓
  사용자 답장 — 번호 선택
         ↓
  openclaw.py — 탈락 감지 + 클립 추출
         ↓
  결과물 (원본명_openclaw/)
    ├─ *_kill_01.mp4
    ├─ timecode_log.csv     ← 프리미어 프로용
    ├─ timecode_log.json
    └─ premiere_markers.txt
         ↓
  텔레그램 완료 알림
```

## 파일 구조

```
~/Desktop/OpenClaw-project/
├── openclaw.py          ← 핵심 감지 엔진
├── watcher.py           ← 네트워크 감시 + 텔레그램 대화
├── notify.py            ← 텔레그램 알림 모듈
├── uploader.py          ← (예비) Drive 업로드 모듈
├── credentials.json     ← Google 서비스 계정 키 (git 제외)
├── .env                 ← 환경 변수 (git 제외)
├── .gitignore
├── watcher.log          ← 자동 생성
└── .processed_files.json ← 처리 완료 기록 (자동 생성)
```

## 설치

```bash
cd ~/Desktop/OpenClaw-project
python -m venv venv
source venv/bin/activate
pip install opencv-python pytesseract requests python-dotenv
brew install tesseract tesseract-lang
```

## .env 설정

```env
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## 실행

```bash
source venv/bin/activate

# 상시 감시 (30초 간격)
python watcher.py

# 1회 스캔 테스트
python watcher.py --once

# 폴링 간격 변경
python watcher.py --poll 60

# 처리 목록 초기화 (전체 재처리)
python watcher.py --reset

# openclaw 단독 실행
python openclaw.py /Volumes/영상\ 녹화2/파일명.mkv
```

## 텔레그램 사용법

watcher가 새 파일을 발견하면:

```
🎬 새 영상 3개 발견!
(최신 순 정렬)

1. 2026-05-15 21-32-54.mkv
     📅 05/15 21:32  💾 8.5GB

2. 2026-05-14 22-57-28.mkv
     📅 05/14 22:57  💾 8.5GB

━━━━━━━━━━━━━━━━━━━
처리할 번호를 답장해주세요
예) 1   1 3   전체
건너뛰려면 스킵
```

| 답장 | 동작 |
|------|------|
| `1` | 1번 파일만 처리 |
| `1 3` | 1번, 3번 처리 |
| `전체` | 전부 처리 |
| `스킵` | 이번 스캔 건너뜀 |

## 결과물

```
원본명_openclaw/
├── 원본명_kill_01.mp4   ← 탈락 클립 (앞뒤 30초)
├── 원본명_kill_02.mp4
├── thumb_01.jpg         ← 감지 순간 썸네일
├── timecode_log.csv     ← 프리미어에서 바로 열기
├── timecode_log.json    ← 자동화용
└── premiere_markers.txt ← 마커 가이드
```

### 프리미어 프로에서 타임코드 찾기
1. 원본 영상 타임라인에 올리기
2. `Ctrl+G` → 타임코드 입력 (예: `00:12:34:00`)
3. `M` 키로 마커 추가

## 버전 히스토리

| 버전 | 내용 |
|------|------|
| v5.0.0 | EasyOCR 제거, pytesseract 단순화 (700줄 → 218줄) |
| v5.1.0 | 개별 클립 분리 + 타임코드 3종 기록 |
| watcher v1 | Google Drive 자동 다운로드 |
| watcher v2 | 텔레그램 선택 방식 도입 |
| watcher v3 | Google Drive 제거, 로컬 네트워크 직접 감시 |
