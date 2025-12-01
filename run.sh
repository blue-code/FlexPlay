#!/bin/bash

echo "================================"
echo "FlexPlay 시작"
echo "================================"

# 현재 디렉토리 확인
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 가상환경 존재 확인
if [ ! -d "venv" ]; then
    echo "❌ 가상환경이 설치되어 있지 않습니다."
    echo "먼저 setup.sh를 실행해주세요:"
    echo "  ./setup.sh"
    exit 1
fi

# 가상환경 활성화
echo "🔌 가상환경 활성화 중..."
source venv/bin/activate

if [ $? -ne 0 ]; then
    echo "❌ 가상환경 활성화에 실패했습니다."
    exit 1
fi

# 필요한 디렉토리 확인
if [ ! -d "/Volumes/SSD/video" ]; then
    echo "⚠️  경고: /Volumes/SSD/video 폴더가 존재하지 않습니다."
    echo "폴더를 생성하거나 영상이 있는지 확인해주세요."
fi

if [ ! -d "static/thumbnails" ]; then
    echo "📁 thumbnails 폴더 생성 중..."
    mkdir -p static/thumbnails
fi

# 서버 시작
echo ""
echo "================================"
echo "🚀 서버 시작 중..."
echo "================================"
echo ""
echo "브라우저에서 다음 주소로 접속하세요:"
echo "  http://localhost:7777"
echo ""
echo "종료하려면 Ctrl+C를 누르세요."
echo ""

python app.py
