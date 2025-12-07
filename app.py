import os
import json
import subprocess
import threading
import uuid
import time
import shutil
import platform
import hashlib
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file, Response, stream_with_context
from urllib.parse import unquote
import mimetypes
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5000 * 1024 * 1024  # 5GB max file size

MIN_SEGMENT_DURATION = 0.05  # seconds; ignore shorter leftovers to avoid zero-length cuts

# 설정
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
THUMBNAILS_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'thumbnails')
HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'history.json')
media_info_cache = {}
FFMPEG_AVAILABLE = shutil.which('ffmpeg') is not None
FFMPEG_WARNING_EMITTED = False

# 썸네일 생성 상태 추적
thumbnail_jobs = set()
thumbnail_jobs_lock = threading.Lock()
thumbnail_workers = threading.Semaphore(2)

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


def get_move_targets():
    """파일 이동 대상 목록 가져오기"""
    config = load_config()
    return config.get('move_targets', [])

def get_cache_settings():
    """캐시 설정 가져오기"""
    config = load_config()
    default_settings = {
        'max_age_days': 7,
        'max_size_gb': 50,
        'cleanup_interval_hours': 24,
        'thumbnail_retention_days': 7
    }
    return config.get('cache_settings', default_settings)

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


def get_thumbnail_filename(folder_path, filename):
    """썸네일 파일명을 안정적으로 생성"""
    base = f"{os.path.abspath(folder_path)}::{filename}"
    digest = hashlib.md5(base.encode('utf-8')).hexdigest()
    return f"{digest}.jpg"


def generate_unique_destination(folder_path, filename):
    """목표 폴더 내에서 겹치지 않는 파일 경로 생성"""
    name, ext = os.path.splitext(filename)
    counter = 0

    while True:
        suffix = f"_{counter}" if counter > 0 else ''
        candidate_name = f"{name}{suffix}{ext}"
        try:
            candidate_path = safe_join(folder_path, candidate_name)
        except ValueError:
            raise ValueError("Invalid destination path")

        if not os.path.exists(candidate_path):
            return candidate_path, candidate_name
        counter += 1


def schedule_thumbnail_generation(video_path, folder_path, filename, thumbnail_path, duration=None):
    """썸네일 생성을 백그라운드로 예약"""
    if not FFMPEG_AVAILABLE:
        global FFMPEG_WARNING_EMITTED
        if not FFMPEG_WARNING_EMITTED:
            print("[Thumbnail] ffmpeg binary not found; skipping thumbnail generation.")
            FFMPEG_WARNING_EMITTED = True
        return False

    job_key = f"{os.path.abspath(folder_path)}::{filename}"

    with thumbnail_jobs_lock:
        if job_key in thumbnail_jobs:
            return True
        thumbnail_jobs.add(job_key)

    def worker():
        try:
            generate_thumbnail(video_path, thumbnail_path, duration)
        finally:
            with thumbnail_jobs_lock:
                thumbnail_jobs.discard(job_key)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return True


def determine_thumbnail_seek(duration):
    if not duration or duration <= 0.5:
        return 0.0

    safe_duration = max(duration - 0.25, 0)
    target = min(max(duration * 0.2, 1.0), safe_duration)
    return round(target, 2)


def generate_thumbnail(video_path, thumbnail_path, duration=None):
    """ffmpeg으로 썸네일 생성"""
    temp_output = f"{thumbnail_path}.tmp"

    thumbnail_workers.acquire()
    try:
        os.makedirs(os.path.dirname(thumbnail_path), exist_ok=True)

        seek_time = determine_thumbnail_seek(duration)

        def run_ffmpeg(seek):
            cmd = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error']
            if seek and seek > 0:
                cmd.extend(['-ss', f"{seek:.2f}"])
            cmd.extend([
                '-i', video_path,
                '-frames:v', '1',
                '-vf', 'thumbnail,scale=320:-1',
                '-q:v', '4',
                '-an',
                '-f', 'image2',
                temp_output
            ])
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                text=True
            )

        try:
            run_ffmpeg(seek_time)
        except subprocess.CalledProcessError as exc:
            error_output = exc.stderr.strip() if exc.stderr else 'unknown ffmpeg error'
            print(f"[Thumbnail] Primary attempt failed at {seek_time}s for {video_path}: {error_output}")
            # 짧은 영상 대비: 0초 지점으로 재시도
            run_ffmpeg(0)

        if os.path.exists(temp_output):
            os.replace(temp_output, thumbnail_path)
    except Exception as exc:
        # ffmpeg 실패 시 임시 파일 제거 후 로그 출력
        if os.path.exists(temp_output):
            os.remove(temp_output)
        print(f"[Thumbnail] Failed to generate for {video_path}: {exc}")
    finally:
        thumbnail_workers.release()


def ensure_thumbnail_ready(video_path, folder_path, filename, source_mtime, duration=None):
    """썸네일 파일이 최신인지 확인하고 필요 시 생성을 예약"""
    if not FFMPEG_AVAILABLE:
        return None, False

    thumbnail_name = get_thumbnail_filename(folder_path, filename)
    thumbnail_path = os.path.join(THUMBNAILS_FOLDER, thumbnail_name)
    thumbnail_url = f"/static/thumbnails/{thumbnail_name}"

    if os.path.exists(thumbnail_path):
        thumb_mtime = os.path.getmtime(thumbnail_path)
        if not source_mtime or thumb_mtime >= source_mtime:
            return thumbnail_url, False

    scheduled = schedule_thumbnail_generation(video_path, folder_path, filename, thumbnail_path, duration)

    if os.path.exists(thumbnail_path):
        # 기존 썸네일이 있으면 우선 제공하고, 백그라운드에서 최신화
        return thumbnail_url, scheduled

    return None, scheduled


def delete_thumbnail_for_video(video_path):
    """영상 파일에 해당하는 썸네일 삭제"""
    folder_path = os.path.dirname(video_path)
    filename = os.path.basename(video_path)
    thumbnail_name = get_thumbnail_filename(folder_path, filename)
    thumbnail_path = os.path.join(THUMBNAILS_FOLDER, thumbnail_name)

    if os.path.exists(thumbnail_path):
        try:
            os.remove(thumbnail_path)
            print(f"[Thumbnail] Removed thumbnail for {video_path}")
        except Exception as exc:
            print(f"[Thumbnail] Failed to delete thumbnail {thumbnail_path}: {exc}")


def cleanup_orphan_thumbnails(retention_days):
    """설정된 보존 기간이 지난 고아 썸네일 삭제"""
    if not os.path.exists(THUMBNAILS_FOLDER):
        return

    folders = get_video_folders()
    valid_thumbnails = set()

    for folder_info in folders:
        folder_path = folder_info.get('path')
        if not folder_path or not os.path.exists(folder_path):
            continue

        try:
            for file in os.listdir(folder_path):
                file_path = os.path.join(folder_path, file)
                if not os.path.isfile(file_path):
                    continue
                ext = os.path.splitext(file)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    valid_thumbnails.add(get_thumbnail_filename(folder_path, file))
        except Exception as exc:
            print(f"[Thumbnail] Failed to scan folder {folder_path}: {exc}")

    retention_seconds = max(retention_days, 0) * 24 * 60 * 60 if retention_days else 0
    now = time.time()
    removed = 0

    for thumb_name in os.listdir(THUMBNAILS_FOLDER):
        thumb_path = os.path.join(THUMBNAILS_FOLDER, thumb_name)
        if not os.path.isfile(thumb_path):
            continue

        if thumb_name in valid_thumbnails:
            continue

        age = now - os.path.getmtime(thumb_path)
        if retention_seconds and age < retention_seconds:
            continue

        try:
            os.remove(thumb_path)
            removed += 1
        except Exception as exc:
            print(f"[Thumbnail] Failed to remove orphan thumbnail {thumb_path}: {exc}")

    if removed:
        print(f"[Thumbnail] Cleaned up {removed} orphan thumbnails")


def get_mime_type(filename):
    """파일 확장자로 MIME 타입 반환"""
    ext = os.path.splitext(filename)[1].lower()
    return MIME_TYPES.get(ext) or mimetypes.guess_type(filename)[0] or 'application/octet-stream'


def parse_float(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def normalize_extension(ext):
    if not ext:
        return ''
    ext = ext.strip().lower()
    if ext and not ext.startswith('.'):
        ext = f'.{ext}'
    return ext


def matches_search_query(video, query):
    if not query:
        return True

    query = query.lower()
    fields = [
        video.get('name', ''),
        video.get('folder', ''),
        video.get('video_codec_info', ''),
        video.get('audio_codec_info', '')
    ]
    return any(query in (field or '').lower() for field in fields)


def get_media_info(file_path):
    """영상 코덱/해상도 등의 메타데이터 추출 (ffprobe 결과를 캐시)"""
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return {}

    cache_entry = media_info_cache.get(file_path)
    if cache_entry and cache_entry['mtime'] == mtime:
        return cache_entry['data']

    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries',
        'format=bit_rate:stream=index,codec_type,codec_name,codec_long_name,width,height,bit_rate,channels,channel_layout',
        '-of', 'json',
        file_path
    ]

    metadata = {}

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        info = json.loads(result.stdout)
        streams = info.get('streams', [])
        format_info = info.get('format', {})

        def parse_bit_rate(value):
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str):
                try:
                    return int(float(value))
                except ValueError:
                    return None
            return None

        metadata['bitrate'] = parse_bit_rate(format_info.get('bit_rate'))
        duration = parse_float(format_info.get('duration'))
        if duration and duration > 0:
            metadata['duration'] = duration

        video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
        if video_stream:
            codec = video_stream.get('codec_name') or video_stream.get('codec_long_name')
            width = video_stream.get('width')
            height = video_stream.get('height')
            resolution = f"{width}x{height}" if width and height else None
            if codec:
                codec_label = codec.upper() if len(codec) <= 6 else codec
                metadata['video_codec_info'] = f"{codec_label} ({resolution})" if resolution else codec_label

        audio_stream = next((s for s in streams if s.get('codec_type') == 'audio'), None)
        if audio_stream:
            codec = audio_stream.get('codec_name') or audio_stream.get('codec_long_name')
            channel_layout = audio_stream.get('channel_layout')
            channels = audio_stream.get('channels')
            channel_desc = channel_layout or (f"{channels}ch" if channels else None)
            if codec:
                codec_label = codec.upper() if len(codec) <= 6 else codec
                metadata['audio_codec_info'] = f"{codec_label} ({channel_desc})" if channel_desc else codec_label

    except Exception:
        metadata = {}

    media_info_cache[file_path] = {'mtime': mtime, 'data': metadata}
    return metadata


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
                    media_info = get_media_info(file_path)
                    thumbnail_url, thumbnail_pending = ensure_thumbnail_ready(
                        file_path,
                        folder_path,
                        file,
                        stat.st_mtime,
                        media_info.get('duration') if media_info else None
                    )
                    video_files.append({
                        'name': file,
                        'path': file,
                        'folder': folder_name,
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        'extension': ext,
                        'thumbnail_url': thumbnail_url,
                        'thumbnail_pending': thumbnail_pending,
                        **media_info
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
    folder_filter = [f for f in folders_param.split(',') if f] if folders_param else None

    search_query = request.args.get('search', '').strip()
    extensions_param = request.args.get('extensions', '').strip()
    include_meta = request.args.get('with_meta') == '1'

    videos = get_video_files(folder_filter)
    history_entries = load_history()
    watched_files = {entry.get('filename') for entry in history_entries if entry.get('filename')}
    for video in videos:
        video['watched'] = video['name'] in watched_files
    available_extensions = []

    if include_meta:
        available_extensions = sorted({v['extension'] for v in videos if v.get('extension')})

    filtered_videos = videos

    if search_query:
        filtered_videos = [v for v in filtered_videos if matches_search_query(v, search_query)]

    if extensions_param:
        ext_filter = {
            normalize_extension(ext)
            for ext in extensions_param.split(',')
            if ext.strip()
        }
        if ext_filter:
            filtered_videos = [v for v in filtered_videos if v.get('extension') in ext_filter]

    if include_meta:
        return jsonify({
            'videos': filtered_videos,
            'meta': {
                'extensions': available_extensions,
                'move_targets': get_move_targets()
            }
        })

    return jsonify(filtered_videos)


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


@app.route('/api/hls/<path:filename>/playlist.m3u8')
def hls_playlist(filename):
    """HLS 플레이리스트 제공 (iOS 최적화)"""
    video_path = find_video_path(filename)

    if not video_path:
        return jsonify({'error': 'Video not found'}), 404

    # HLS 캐시 디렉토리
    hls_dir = os.path.join(os.path.dirname(__file__), 'static', 'hls')
    os.makedirs(hls_dir, exist_ok=True)

    # 파일명에서 안전한 디렉토리명 생성
    name, ext = os.path.splitext(filename)
    safe_name = "".join(c if c.isalnum() or c in ('_', '-') else '_' for c in name)
    video_hls_dir = os.path.join(hls_dir, safe_name)
    os.makedirs(video_hls_dir, exist_ok=True)

    playlist_path = os.path.join(video_hls_dir, 'playlist.m3u8')

    # HLS 파일이 없거나 원본보다 오래된 경우 생성
    if not os.path.exists(playlist_path) or os.path.getmtime(video_path) > os.path.getmtime(playlist_path):
        # FFmpeg로 HLS 생성 (iOS 호환)
        ffmpeg_cmd = [
            'ffmpeg', '-y',
            '-i', video_path,
            '-c:v', 'libx264',              # H.264 비디오
            '-profile:v', 'baseline',        # iOS 호환 프로필
            '-level', '3.0',                 # iOS 호환 레벨
            '-preset', 'fast',               # 빠른 인코딩
            '-crf', '23',                    # 품질
            '-maxrate', '3M',                # 최대 비트레이트
            '-bufsize', '6M',                # 버퍼 크기
            '-pix_fmt', 'yuv420p',           # iOS 필수
            '-c:a', 'aac',                   # AAC 오디오
            '-b:a', '128k',
            '-ar', '44100',
            '-hls_time', '6',                # 세그먼트 길이 (6초)
            '-hls_list_size', '0',           # 모든 세그먼트 목록에 포함
            '-hls_segment_type', 'mpegts',   # MPEG-TS 세그먼트
            '-hls_segment_filename', os.path.join(video_hls_dir, 'segment_%03d.ts'),
            '-f', 'hls',
            playlist_path
        ]

        try:
            subprocess.run(
                ffmpeg_cmd,
                capture_output=True,
                check=True,
                timeout=600
            )
        except subprocess.CalledProcessError as e:
            return jsonify({
                'error': 'HLS generation failed',
                'details': e.stderr.decode('utf-8') if e.stderr else 'Unknown error'
            }), 500
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'HLS generation timeout'}), 500

    # 플레이리스트 파일 제공
    return send_file(playlist_path, mimetype='application/vnd.apple.mpegurl')


@app.route('/api/hls/<path:filename>/<segment_name>')
def hls_segment(filename, segment_name):
    """HLS 세그먼트 파일 제공"""
    # 파일명에서 안전한 디렉토리명 생성
    name, ext = os.path.splitext(filename)
    safe_name = "".join(c if c.isalnum() or c in ('_', '-') else '_' for c in name)

    hls_dir = os.path.join(os.path.dirname(__file__), 'static', 'hls')
    video_hls_dir = os.path.join(hls_dir, safe_name)
    segment_path = os.path.join(video_hls_dir, segment_name)

    if not os.path.exists(segment_path):
        return jsonify({'error': 'Segment not found'}), 404

    return send_file(segment_path, mimetype='video/mp2t')


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

        name, ext = os.path.splitext(filename)

        # 트랜스코딩된 캐시 파일도 삭제
        cache_dir = os.path.join(os.path.dirname(__file__), 'static', 'transcoded')
        cache_filename = f"{name}_transcoded.mp4"
        cache_path = os.path.join(cache_dir, cache_filename)

        if os.path.exists(cache_path):
            os.remove(cache_path)

        # HLS 캐시 디렉토리도 삭제
        hls_dir = os.path.join(os.path.dirname(__file__), 'static', 'hls')
        safe_name = "".join(c if c.isalnum() or c in ('_', '-') else '_' for c in name)
        video_hls_dir = os.path.join(hls_dir, safe_name)

        if os.path.exists(video_hls_dir):
            shutil.rmtree(video_hls_dir)

        # 히스토리에서도 제거
        decoded_filename = unquote(filename)
        history = load_history()
        history = [h for h in history if h.get('filename') != decoded_filename]
        save_history(history)

        delete_thumbnail_for_video(video_path)

        return jsonify({'success': True, 'message': 'Video deleted successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/move', methods=['POST'])
def move_video_to_target():
    """영상 파일을 지정된 이동 대상 폴더로 이동 (단일 또는 다중)"""
    data = request.get_json() or {}
    filename = data.get('filename')
    filenames = data.get('filenames')
    target_name = data.get('target')

    if not target_name:
        return jsonify({'error': 'target is required'}), 400

    target_candidates = get_move_targets()
    target_entry = next((t for t in target_candidates if t.get('name') == target_name), None)
    if not target_entry:
        return jsonify({'error': 'Invalid target'}), 400

    target_folder = target_entry.get('path')
    if not target_folder:
        return jsonify({'error': 'Target path not configured'}), 400

    os.makedirs(target_folder, exist_ok=True)

    def move_single(name):
        video_path = find_video_path(name)
        if not video_path or not os.path.exists(video_path):
            return {'filename': name, 'success': False, 'error': 'Video not found'}

        try:
            destination_path, destination_name = generate_unique_destination(
                target_folder,
                os.path.basename(name)
            )
        except ValueError:
            return {'filename': name, 'success': False, 'error': 'Failed to resolve destination path'}

        try:
            shutil.move(video_path, destination_path)
            delete_thumbnail_for_video(video_path)
        except Exception as exc:
            return {'filename': name, 'success': False, 'error': f'Failed to move file: {exc}'}

        return {
            'filename': name,
            'success': True,
            'destination': destination_path,
            'destination_name': destination_name
        }

    # 다중 이동 처리
    if filenames:
        if not isinstance(filenames, list):
            return jsonify({'error': 'filenames must be a list'}), 400

        unique_names = []
        seen = set()
        for name in filenames:
            if not isinstance(name, str):
                continue
            if name in seen:
                continue
            seen.add(name)
            unique_names.append(name)

        if not unique_names:
            return jsonify({'error': 'No valid filenames provided'}), 400

        results = [move_single(name) for name in unique_names]
        success_count = sum(1 for r in results if r.get('success'))
        failure_count = len(results) - success_count

        status_code = 200 if failure_count == 0 else 207  # 207: multi-status like response
        return jsonify({
            'success': failure_count == 0,
            'moved': [r for r in results if r.get('success')],
            'failed': [r for r in results if not r.get('success')],
            'summary': {
                'requested': len(unique_names),
                'moved': success_count,
                'failed': failure_count
            }
        }), status_code

    # 단일 이동 처리 (기존 호환)
    if not filename:
        return jsonify({'error': 'filename or filenames is required'}), 400

    result = move_single(filename)
    if not result.get('success'):
        return jsonify({'error': result.get('error', 'Failed to move file')}), 400

    return jsonify({
        'success': True,
        'destination': result['destination'],
        'destination_name': result['destination_name']
    })


def get_directory_size(directory):
    """디렉토리의 총 크기 계산 (바이트)"""
    total_size = 0
    try:
        for dirpath, dirnames, filenames in os.walk(directory):
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.exists(filepath):
                    total_size += os.path.getsize(filepath)
    except Exception as e:
        print(f"Error calculating directory size: {e}")
    return total_size


def cleanup_old_cache():
    """오래된 캐시 파일 자동 정리"""
    try:
        settings = get_cache_settings()
        max_age_days = settings['max_age_days']
        max_size_gb = settings['max_size_gb']
        thumbnail_retention_days = settings.get('thumbnail_retention_days', max_age_days)

        cache_dirs = [
            os.path.join(os.path.dirname(__file__), 'static', 'transcoded'),
            os.path.join(os.path.dirname(__file__), 'static', 'hls')
        ]

        now = time.time()
        max_age_seconds = max_age_days * 24 * 60 * 60
        deleted_count = 0
        freed_space = 0

        for cache_dir in cache_dirs:
            if not os.path.exists(cache_dir):
                continue

            # 오래된 파일 삭제 (접근 시간 기준)
            for item in os.listdir(cache_dir):
                item_path = os.path.join(cache_dir, item)

                try:
                    # 파일 또는 디렉토리의 마지막 접근 시간
                    atime = os.path.getatime(item_path)
                    age_seconds = now - atime

                    if age_seconds > max_age_seconds:
                        item_size = 0
                        if os.path.isdir(item_path):
                            item_size = get_directory_size(item_path)
                            shutil.rmtree(item_path)
                        else:
                            item_size = os.path.getsize(item_path)
                            os.remove(item_path)

                        deleted_count += 1
                        freed_space += item_size
                        print(f"Deleted old cache: {item_path}")
                except Exception as e:
                    print(f"Error deleting {item_path}: {e}")

        # 전체 캐시 크기 확인 및 용량 제한
        total_cache_size = sum(get_directory_size(d) for d in cache_dirs if os.path.exists(d))
        max_size_bytes = max_size_gb * 1024 * 1024 * 1024

        if total_cache_size > max_size_bytes:
            # LRU (Least Recently Used) 방식으로 삭제
            all_cache_items = []

            for cache_dir in cache_dirs:
                if not os.path.exists(cache_dir):
                    continue

                for item in os.listdir(cache_dir):
                    item_path = os.path.join(cache_dir, item)
                    try:
                        atime = os.path.getatime(item_path)
                        if os.path.isdir(item_path):
                            size = get_directory_size(item_path)
                        else:
                            size = os.path.getsize(item_path)
                        all_cache_items.append((item_path, atime, size))
                    except:
                        pass

            # 접근 시간 순으로 정렬 (오래된 것부터)
            all_cache_items.sort(key=lambda x: x[1])

            # 용량이 제한 이하가 될 때까지 삭제
            current_size = total_cache_size
            for item_path, _, item_size in all_cache_items:
                if current_size <= max_size_bytes:
                    break

                try:
                    if os.path.isdir(item_path):
                        shutil.rmtree(item_path)
                    else:
                        os.remove(item_path)

                    current_size -= item_size
                    deleted_count += 1
                    freed_space += item_size
                    print(f"Deleted cache (size limit): {item_path}")
                except Exception as e:
                    print(f"Error deleting {item_path}: {e}")

        cleanup_orphan_thumbnails(thumbnail_retention_days)

        if deleted_count > 0:
            print(f"Cache cleanup completed: {deleted_count} items deleted, {freed_space / (1024*1024):.2f} MB freed")
        else:
            print("Cache cleanup: No old files to delete")

    except Exception as e:
        print(f"Error during cache cleanup: {e}")


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


def get_edit_codec_args():
    """편집 시 사용할 비디오 코덱 인코딩 옵션 (맥은 GPU 활용)"""
    if platform.system() == 'Darwin':
        primary = [
            '-c:v', 'hevc_videotoolbox',
            '-tag:v', 'hvc1',
            '-b:v', '5M',
            '-maxrate', '6M',
            '-bufsize', '12M',
            '-pix_fmt', 'yuv420p'
        ]
        fallback = [
            '-c:v', 'libx265',
            '-preset', 'medium',
            '-crf', '22',
            '-pix_fmt', 'yuv420p'
        ]
        return primary, fallback

    primary = [
        '-c:v', 'libx265',
        '-preset', 'medium',
        '-crf', '22',
        '-pix_fmt', 'yuv420p'
    ]
    return primary, None


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

        codec_args, fallback_codec_args = get_edit_codec_args()

        for i, segment in enumerate(keep_segments):
            # 각 구간 추출
            segment_duration = segment['end'] - segment['start']
            if segment_duration <= 0:
                continue
            if segment_duration < MIN_SEGMENT_DURATION:
                # FFmpeg가 처리하지 못하는 초미세 구간은 무시
                continue

            temp_file = os.path.join(temp_dir, f"temp_segment_{i}_{task_id}.mp4")
            temp_files.append(temp_file)

            base_cmd = [
                'ffmpeg', '-y',
                '-ss', str(segment['start']),
                '-i', video_path,
                '-t', str(segment_duration)
            ]

            cmd = base_cmd + codec_args + [
                '-c:a', 'copy',
                '-movflags', '+faststart',
                temp_file
            ]

            try:
                subprocess.run(cmd, capture_output=True, check=True)
            except subprocess.CalledProcessError:
                if fallback_codec_args:
                    fallback_cmd = base_cmd + fallback_codec_args + [
                        '-c:a', 'copy',
                        '-movflags', '+faststart',
                        temp_file
                    ]
                    subprocess.run(fallback_cmd, capture_output=True, check=True)
                else:
                    raise
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


# 캐시 자동 정리 스케줄러 설정
scheduler = BackgroundScheduler()
settings = get_cache_settings()
cleanup_interval_hours = settings['cleanup_interval_hours']

# 정리 작업 스케줄 (설정된 간격마다 실행)
scheduler.add_job(
    func=cleanup_old_cache,
    trigger='interval',
    hours=cleanup_interval_hours,
    id='cache_cleanup',
    name='Automatic cache cleanup',
    replace_existing=True
)

# 서버 시작 시 한 번 실행
scheduler.add_job(
    func=cleanup_old_cache,
    trigger='date',
    id='cache_cleanup_startup',
    name='Cache cleanup on startup',
    replace_existing=True
)

scheduler.start()


if __name__ == '__main__':
    try:
        print(f"🗑️  캐시 자동 정리: {cleanup_interval_hours}시간마다 실행 (최대 {settings['max_age_days']}일, {settings['max_size_gb']}GB)")
        app.run(debug=True, host='0.0.0.0', port=7777, threaded=True)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
