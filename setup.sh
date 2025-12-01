#!/bin/bash

echo "================================"
echo "FlexPlay 설치 시작"
echo "================================"

# 현재 디렉토리 확인
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Python 버전 확인
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3가 설치되어 있지 않습니다."
    echo "Python 3를 먼저 설치해주세요: https://www.python.org/downloads/"
    exit 1
fi

echo "✅ Python 버전: $(python3 --version)"

# 가상환경이 이미 존재하는지 확인
if [ -d "venv" ]; then
    echo "⚠️  기존 가상환경이 발견되었습니다."
    read -p "기존 가상환경을 삭제하고 새로 설치하시겠습니까? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo "🗑️  기존 가상환경 삭제 중..."
        rm -rf venv
    else
        echo "기존 가상환경을 유지합니다."
    fi
fi

# 가상환경 생성
if [ ! -d "venv" ]; then
    echo "📦 Python 가상환경 생성 중..."
    python3 -m venv venv

    if [ $? -ne 0 ]; then
        echo "❌ 가상환경 생성에 실패했습니다."
        exit 1
    fi
    echo "✅ 가상환경 생성 완료"
fi

# 가상환경 활성화
echo "🔌 가상환경 활성화 중..."
source venv/bin/activate

if [ $? -ne 0 ]; then
    echo "❌ 가상환경 활성화에 실패했습니다."
    exit 1
fi

# pip 업그레이드
echo "⬆️  pip 업그레이드 중..."
pip install --upgrade pip

# requirements.txt에서 패키지 설치
echo "📚 필요한 패키지 설치 중..."
pip install -r requirements.txt

if [ $? -ne 0 ]; then
    echo "❌ 패키지 설치에 실패했습니다."
    exit 1
fi

# 필요한 디렉토리 생성
echo "📁 필요한 디렉토리 생성 중..."
mkdir -p static/thumbnails

echo ""
echo "================================"
echo "✅ 설치 완료!"
echo "================================"
echo ""
echo "실행 방법:"
echo "  ./run.sh"
echo ""
echo "또는:"
echo "  source venv/bin/activate"
echo "  python app.py"
echo ""
echo "영상 폴더: /Volumes/SSD/video"
echo "================================"
