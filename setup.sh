#!/bin/bash
set -e
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'
echo ""
echo "╔══════════════════════════════════════╗"
echo "║   🦞  OpenClaw 환경 셋업             ║"
echo "╚══════════════════════════════════════╝"
echo ""
if ! command -v brew &>/dev/null; then
    echo -e "${RED}[ERROR] Homebrew 없음. https://brew.sh 설치 후 재시도${NC}"
    exit 1
fi
command -v ffmpeg &>/dev/null || brew install ffmpeg
command -v tesseract &>/dev/null || brew install tesseract tesseract-lang
pip3 install -r requirements.txt --break-system-packages --quiet
mkdir -p ~/Desktop/OpenClaw/input/chunks
mkdir -p ~/Desktop/OpenClaw/input/clips
mkdir -p ~/Desktop/OpenClaw/output/thumbs
echo -e "${GREEN}✅ 셋업 완료!${NC}"
echo ""
echo "사용법:"
echo "  1. vod_latest.mp4 → ~/Desktop/OpenClaw/input/ 에 복사"
echo "  2. python3 openclaw.py 실행"
