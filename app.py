import os
import json
import subprocess
import threading
import uuid
import time
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file, Response, stream_with_context
from urllib.parse import unquote
import mimetypes
from datetime import datetime

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5000 * 1024 * 1024  # 5GB max file size

# 설정
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
THUMBNAILS_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'thumbnails')
HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'history.json')

def load_config():
    """설정 파일 로드"""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {'video_folders': []}
    return {'video_folders': []}

def get_video_folders():
    """영상 폴더 목록 가져오기"""
    config = load_config()
    return config.get('video_folders', [])

# 지원하는 영상 파일 확장자 (광범위한 코덱 지원)
VIDEO_EXTENSIONS = {
    # 일반적인 형식
    '.mp4', '.m4v', '.mov',  # MPEG-4, H.264, H.265
    '.avi',  # AVI
    '.mkv',  # Matroska
    '.webm',  # WebM
    '.flv', '.f4v',  # Flash Video
    '.wmv', '.asf',  # Windows Media

    # MPEG 계열
    '.mpg', '.mpeg', '.mpe',  # MPEG-1, MPEG-2
    '.m2v', '.m4p',
    '.ts', '.mts', '.m2ts',  # MPEG Transport Stream, AVCHD

    # 모바일/스트리밍
    '.3gp', '.3g2',  # 3GPP

    # 오픈 소스 형식
    '.ogv', '.ogg', '.ogm',  # Ogg Video

    # 구형/전문가용 형식
    '.vob',  # DVD
    '.rm', '.rmvb',  # RealMedia
    '.divx',  # DivX
    '.mxf',  # Material Exchange Format
    '.mod', '.tod',  # JVC/Panasonic
    '.dat',  # VCD

    # Apple/QuickTime
    '.qt',

    # 기타
    '.dv',  # Digital Video
    '.amv',  # AMV
}

# MIME 타입 매핑 (브라우저 호환성 개선)
MIME_TYPES = {
    '.mp4': 'video/mp4',
    '.m4v': 'video/mp4',
    '.mov': 'video/quicktime',
    '.qt': 'video/quicktime',
    '.avi': 'video/x-msvideo',
    '.mkv': 'video/x-matroska',
    '.webm': 'video/webm',
    '.flv': 'video/x-flv',
    '.f4v': 'video/mp4',
    '.wmv': 'video/x-ms-wmv',
    '.asf': 'video/x-ms-asf',
    '.mpg': 'video/mpeg',
    '.mpeg': 'video/mpeg',
    '.mpe': 'video/mpeg',
    '.m2v': 'video/mpeg',
    '.ts': 'video/mp2t',
    '.mts': 'video/mp2t',
    '.m2ts': 'video/mp2t',
    '.3gp': 'video/3gpp',
    '.3g2': 'video/3gpp2',
    '.ogv': 'video/ogg',
    '.ogg': 'video/ogg',
    '.ogm': 'video/ogg',
    '.vob': 'video/mpeg',
    '.rm': 'application/vnd.rn-realmedia',
    '.rmvb': 'application/vnd.rn-realmedia-vbr',
    '.divx': 'video/x-msvideo',
    '.dv': 'video/x-dv',
    '.mxf': 'application/mxf',
}

# 필요한 폴더 생성 (썸네일 폴더만)
os.makedirs(THUMBNAILS_FOLDER, exist_ok=True)

# 편집 작업 추적
edit_tasks = {}  # task_id -> {status, progress, output_file, error}


def safe_join(base_path, filename):
    """경로 조작 공격을 방지하면서 파일명 결합"""
    # URL 디코딩
    filename = unquote(filename)

    # 경로 구분자 제거 (디렉토리 탐색 방지)
    filename = os.path.basename(filename)

    # 절대 경로 생성
    filepath = os.path.join(base_path, filename)

    # 정규화된 경로가 base_path 내부에 있는지 확인
    base_abs = os.path.abspath(base_path)
    file_abs = os.path.abspath(filepath)

    # 공통 경로가 base_path인지 확인
    if not file_abs.startswith(base_abs + os.sep) and file_abs != base_abs:
        raise ValueError("Invalid file path")

    return filepath


def find_video_path(filename):
    """모든 폴더에서 영상 파일 찾기"""
    folders = get_video_folders()
    for folder in folders:
        try:
            video_path = safe_join(folder['path'], filename)
            if os.path.exists(video_path):
                return video_path
        except:
            continue
    return None


def get_mime_type(filename):
    """파일 확장자로 MIME 타입 반환"""
    ext = os.path.splitext(filename)[1].lower()
    return MIME_TYPES.get(ext) or mimetypes.guess_type(filename)[0] or 'application/octet-stream'


def get_video_files(folder_filter=None):
    """영상 파일 목록 가져오기

    Args:
        folder_filter: 폴더명 목록 (None이면 전체, 빈 리스트면 전체, 특정 폴더명들이면 해당 폴더만)
    """
    video_files = []
    folders = get_video_folders()

    for folder_info in folders:
        folder_name = folder_info['name']
        folder_path = folder_info['path']

        # 폴더 필터링
        if folder_filter and len(folder_filter) > 0 and folder_name not in folder_filter:
            continue

        if not os.path.exists(folder_path):
            continue

        for file in os.listdir(folder_path):
            file_path = os.path.join(folder_path, file)
            if os.path.isfile(file_path):
                ext = os.path.splitext(file)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    stat = os.stat(file_path)
                    video_files.append({
                        'name': file,
                        'path': file,
                        'folder': folder_name,
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        'extension': ext
                    })

    # 수정 날짜 기준 정렬
    video_files.sort(key=lambda x: x['modified'], reverse=True)
    return video_files


def load_history():
    """재생 기록 불러오기"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return []
    return []


def save_history(history):
    """재생 기록 저장하기"""
    with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


@app.route('/')
def index():
    """메인 페이지"""
    return render_template('index.html')


@app.route('/api/folders')
def get_folders():
    """폴더 목록 API (영상 갯수 포함)"""
    folders = get_video_folders()
    folder_list = []

    for folder_info in folders:
        folder_name = folder_info['name']
        folder_path = folder_info['path']

        # 영상 갯수 계산
        video_count = 0
        if os.path.exists(folder_path):
            for file in os.listdir(folder_path):
                file_path = os.path.join(folder_path, file)
                if os.path.isfile(file_path):
                    ext = os.path.splitext(file)[1].lower()
                    if ext in VIDEO_EXTENSIONS:
                        video_count += 1

        folder_list.append({
            'name': folder_name,
            'path': folder_path,
            'count': video_count
        })

    return jsonify(folder_list)


@app.route('/api/videos')
def get_videos():
    """영상 목록 API"""
    # 쿼리 파라미터에서 폴더 필터 가져오기
    folders_param = request.args.get('folders', '')
    folder_filter = folders_param.split(',') if folders_param else None

    videos = get_video_files(folder_filter)
    return jsonify(videos)


@app.route('/api/video/<path:filename>')
def serve_video(filename):
    """영상 스트리밍"""
    video_path = find_video_path(filename)

    if not video_path:
        return jsonify({'error': 'Video not found'}), 404

    # Range request 지원 (영상 탐색을 위해)
    range_header = request.headers.get('Range', None)

    if not range_header:
        return send_file(video_path)

    size = os.path.getsize(video_path)
    byte_start, byte_end = 0, size - 1

    if range_header:
        byte_range = range_header.replace('bytes=', '').split('-')
        byte_start = int(byte_range[0])
        if byte_range[1]:
            byte_end = int(byte_range[1])

    length = byte_end - byte_start + 1

    with open(video_path, 'rb') as f:
        f.seek(byte_start)
        data = f.read(length)

    response = Response(
        data,
        206,
        mimetype=get_mime_type(filename),
        direct_passthrough=True
    )

    response.headers.add('Content-Range', f'bytes {byte_start}-{byte_end}/{size}')
    response.headers.add('Accept-Ranges', 'bytes')
    response.headers.add('Content-Length', str(length))

    return response


@app.route('/api/transcode/<path:filename>')
def transcode_video(filename):
    """트랜스코딩 스트리밍 (iOS 호환 - 캐시 기반)"""
    video_path = find_video_path(filename)

    if not video_path:
        return jsonify({'error': 'Video not found'}), 404

    # 트랜스코딩된 파일 캐시 경로
    cache_dir = os.path.join(os.path.dirname(__file__), 'static', 'transcoded')
    os.makedirs(cache_dir, exist_ok=True)

    # 캐시 파일명 생성 (원본 파일명 기반)
    name, ext = os.path.splitext(filename)
    cache_filename = f"{name}_transcoded.mp4"
    cache_path = os.path.join(cache_dir, cache_filename)

    # 캐시 파일이 없거나 원본보다 오래된 경우 트랜스코딩
    if not os.path.exists(cache_path) or os.path.getmtime(video_path) > os.path.getmtime(cache_path):
        # FFmpeg 명령어: iOS 호환 H.264 + AAC로 트랜스코딩
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-c:v', 'libx264',           # H.264 비디오 코덱
            '-profile:v', 'baseline',     # Baseline profile (최대 호환성)
            '-level', '3.0',              # Level 3.0 (iOS 호환)
            '-preset', 'fast',            # 빠른 인코딩
            '-crf', '23',                 # 품질 (18-28, 낮을수록 고화질)
            '-maxrate', '2M',             # 최대 비트레이트
            '-bufsize', '4M',             # 버퍼 크기
            '-pix_fmt', 'yuv420p',        # Pixel format (iOS 필수)
            '-c:a', 'aac',                # AAC 오디오 코덱
            '-b:a', '128k',               # 오디오 비트레이트
            '-ar', '44100',               # 오디오 샘플레이트
            '-movflags', '+faststart',    # 웹 스트리밍 최적화
            '-f', 'mp4',                  # MP4 형식
            cache_path
        ]

        try:
            # 트랜스코딩 실행
            result = subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                check=True,
                timeout=600  # 10분 타임아웃
            )
        except subprocess.CalledProcessError as e:
            return jsonify({
                'error': 'Transcoding failed',
                'details': e.stderr.decode('utf-8') if e.stderr else 'Unknown error'
            }), 500
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Transcoding timeout'}), 500

    # Range request 지원으로 캐시 파일 제공
    range_header = request.headers.get('Range', None)

    if not range_header:
        return send_file(cache_path, mimetype='video/mp4')

    size = os.path.getsize(cache_path)
    byte_start, byte_end = 0, size - 1

    if range_header:
        byte_range = range_header.replace('bytes=', '').split('-')
        byte_start = int(byte_range[0])
        if byte_range[1]:
            byte_end = int(byte_range[1])

    length = byte_end - byte_start + 1

    with open(cache_path, 'rb') as f:
        f.seek(byte_start)
        data = f.read(length)

    response = Response(
        data,
        206,
        mimetype='video/mp4',
        direct_passthrough=True
    )

    response.headers.add('Content-Range', f'bytes {byte_start}-{byte_end}/{size}')
    response.headers.add('Accept-Ranges', 'bytes')
    response.headers.add('Content-Length', str(length))

    return response


@app.route('/api/delete/<path:filename>', methods=['DELETE'])
def delete_video(filename):
    """영상 파일 삭제"""
    video_path = find_video_path(filename)

    if not video_path:
        return jsonify({'error': 'Video not found'}), 404

    try:
        # 원본 파일 삭제
        os.remove(video_path)

        # 트랜스코딩된 캐시 파일도 삭제
        cache_dir = os.path.join(os.path.dirname(__file__), 'static', 'transcoded')
        name, ext = os.path.splitext(filename)
        cache_filename = f"{name}_transcoded.mp4"
        cache_path = os.path.join(cache_dir, cache_filename)

        if os.path.exists(cache_path):
            os.remove(cache_path)

        # 히스토리에서도 제거
        decoded_filename = unquote(filename)
        history = load_history()
        history = [h for h in history if h.get('filename') != decoded_filename]
        save_history(history)

        return jsonify({'success': True, 'message': 'Video deleted successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/history', methods=['GET', 'POST'])
def handle_history():
    """재생 기록 관리"""
    if request.method == 'GET':
        history = load_history()
        return jsonify(history)

    elif request.method == 'POST':
        data = request.get_json()
        history = load_history()

        # 중복 제거 (같은 영상이면 최신 기록만 유지)
        history = [h for h in history if h.get('filename') != data.get('filename')]

        # 새 기록 추가
        history.insert(0, {
            'filename': data.get('filename'),
            'timestamp': datetime.now().isoformat(),
            'position': data.get('position', 0)
        })

        # 최대 50개까지만 유지
        history = history[:50]

        save_history(history)
        return jsonify({'success': True})


@app.route('/api/thumbnail/<path:filename>')
def get_thumbnail(filename):
    """영상 썸네일 제공 (간단한 구현)"""
    # 실제로는 moviepy나 ffmpeg를 사용하여 썸네일을 생성할 수 있습니다
    # 여기서는 기본 이미지를 반환하거나, 썸네일이 없으면 생성하는 로직을 추가할 수 있습니다
    thumbnail_path = os.path.join(THUMBNAILS_FOLDER, f"{os.path.splitext(filename)[0]}.jpg")

    if os.path.exists(thumbnail_path):
        return send_file(thumbnail_path)

    # 썸네일이 없으면 기본 이미지 반환 (또는 404)
    return jsonify({'error': 'Thumbnail not found'}), 404


def process_video_edit(task_id, video_path, segments, output_path):
    """백그라운드에서 영상 편집 처리"""
    try:
        edit_tasks[task_id]['status'] = 'processing'
        edit_tasks[task_id]['progress'] = 0

        # FFmpeg filter_complex를 사용하여 구간 삭제
        # 구간들을 역순으로 정렬하여 keep할 구간들을 추출
        duration_cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                       '-of', 'default=noprint_wrappers=1:nokey=1', video_path]
        result = subprocess.run(duration_cmd, capture_output=True, text=True)
        total_duration = float(result.stdout.strip())

        # Keep할 구간들 계산
        keep_segments = []
        last_end = 0

        for segment in sorted(segments, key=lambda x: x['start']):
            if segment['start'] > last_end:
                keep_segments.append({'start': last_end, 'end': segment['start']})
            last_end = segment['end']

        # 마지막 구간 추가
        if last_end < total_duration:
            keep_segments.append({'start': last_end, 'end': total_duration})

        if not keep_segments:
            raise ValueError("모든 영상이 삭제됩니다. 최소 일부 구간은 유지되어야 합니다.")

        # 임시 파일들 생성
        temp_files = []
        temp_dir = os.path.dirname(output_path)

        for i, segment in enumerate(keep_segments):
            temp_file = os.path.join(temp_dir, f"temp_segment_{i}_{task_id}.mp4")
            temp_files.append(temp_file)

            # 각 구간 추출
            cmd = [
                'ffmpeg', '-y',
                '-i', video_path,
                '-ss', str(segment['start']),
                '-to', str(segment['end']),
                '-c', 'copy',
                temp_file
            ]

            subprocess.run(cmd, capture_output=True, check=True)
            edit_tasks[task_id]['progress'] = int((i + 1) / (len(keep_segments) + 1) * 90)

        # concat 리스트 파일 생성
        concat_file = os.path.join(temp_dir, f"concat_{task_id}.txt")
        with open(concat_file, 'w') as f:
            for temp_file in temp_files:
                f.write(f"file '{os.path.basename(temp_file)}'\n")

        # 파일들을 하나로 합치기
        concat_cmd = [
            'ffmpeg', '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', concat_file,
            '-c', 'copy',
            output_path
        ]

        subprocess.run(concat_cmd, capture_output=True, check=True, cwd=temp_dir)

        # 임시 파일 정리
        for temp_file in temp_files:
            if os.path.exists(temp_file):
                os.remove(temp_file)
        if os.path.exists(concat_file):
            os.remove(concat_file)

        edit_tasks[task_id]['status'] = 'completed'
        edit_tasks[task_id]['progress'] = 100
        edit_tasks[task_id]['output_file'] = os.path.basename(output_path)

    except Exception as e:
        edit_tasks[task_id]['status'] = 'error'
        edit_tasks[task_id]['error'] = str(e)
        # 임시 파일 정리
        for temp_file in temp_files if 'temp_files' in locals() else []:
            if os.path.exists(temp_file):
                os.remove(temp_file)


@app.route('/api/edit', methods=['POST'])
def start_edit():
    """영상 편집 시작"""
    try:
        data = request.get_json()
        filename = data.get('filename')
        segments = data.get('segments', [])

        if not filename or not segments:
            return jsonify({'error': 'Invalid parameters'}), 400

        video_path = find_video_path(filename)

        if not video_path:
            return jsonify({'error': 'Video not found'}), 404

        # 새 파일명 생성 (같은 폴더에 저장)
        video_dir = os.path.dirname(video_path)
        name, ext = os.path.splitext(filename)
        output_filename = f"{name}_edited_{int(time.time())}{ext}"
        output_path = os.path.join(video_dir, output_filename)

        # 작업 ID 생성
        task_id = str(uuid.uuid4())

        # 작업 정보 초기화
        edit_tasks[task_id] = {
            'status': 'pending',
            'progress': 0,
            'output_file': None,
            'error': None
        }

        # 백그라운드 스레드에서 편집 처리
        thread = threading.Thread(
            target=process_video_edit,
            args=(task_id, video_path, segments, output_path)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'task_id': task_id,
            'message': 'Edit task started'
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/edit/status/<task_id>')
def get_edit_status(task_id):
    """편집 작업 진행 상황 확인"""
    if task_id not in edit_tasks:
        return jsonify({'error': 'Task not found'}), 404

    return jsonify(edit_tasks[task_id])


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=7777, threaded=True)
