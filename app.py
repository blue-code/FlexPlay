import os
import json
import subprocess
import threading
import queue
import uuid
import time
import shutil
import platform
import hashlib
from pathlib import Path
from flask import (
    Flask,
    render_template,
    jsonify,
    request,
    send_file,
    Response,
    stream_with_context,
    session,
    redirect,
    url_for
)
from urllib.parse import unquote
import mimetypes
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 5000 * 1024 * 1024  # 5GB max file size
app.secret_key = os.environ.get('FLEXPLAY_SECRET_KEY', 'change-me-for-production')
app.permanent_session_lifetime = timedelta(days=30)

MIN_SEGMENT_DURATION = 0.05  # seconds; ignore shorter leftovers to avoid zero-length cuts
WATCH_COMPLETE_RATIO = 0.9
WATCH_COMPLETE_OFFSET = 30  # seconds

# 설정
CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
THUMBNAILS_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'thumbnails')
HISTORY_FILE = os.path.join(os.path.dirname(__file__), 'history.json')
media_info_cache = {}
FFMPEG_AVAILABLE = shutil.which('ffmpeg') is not None
FFMPEG_WARNING_EMITTED = False
LOGIN_USERNAME = 'guest'
LOGIN_PASSWORD = 'sam927'
FAILED_ATTEMPT_LIMIT = 5
failed_attempt_count = 0
last_failed_ip = None
login_locked = False

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
        'thumbnail_retention_days': 7,
        # 무소음 전체재생용 자동 트랜스코딩 캐시는 임시성이 강하므로 더 짧게 보존.
        # 마지막 사용(접근) 후 이 시간이 지나면 자동 삭제. (시간 단위)
        'silent_transcode_retention_hours': 24
    }
    # 사용자 설정과 기본값을 병합해 일부 키만 지정해도 누락되지 않도록 한다.
    merged = dict(default_settings)
    merged.update(config.get('cache_settings', {}) or {})
    return merged

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

# 브라우저(특히 iOS/macOS Safari)에서 별도 트랜스코딩 없이 재생 가능한 코덱/컨테이너
# 아래 조건을 모두 만족하면 원본 그대로 재생, 하나라도 어긋나면 트랜스코딩(또는 리먹스) 필요
BROWSER_COMPATIBLE_VIDEO_CODECS = {'h264', 'avc', 'avc1', 'hevc', 'h265', 'hvc1'}
BROWSER_COMPATIBLE_AUDIO_CODECS = {'aac', 'mp4a', 'mp3', 'mp4a.40.2', 'alac'}
BROWSER_COMPATIBLE_CONTAINERS = {'.mp4', '.m4v', '.mov', '.qt'}
# 스트림 복사(copy)만으로 mp4 컨테이너에 담아 재생 가능한 코덱 (재인코딩 불필요 = 가장 빠름)
COPYABLE_VIDEO_CODECS = {'h264', 'avc', 'avc1', 'hevc', 'h265', 'hvc1'}
COPYABLE_AUDIO_CODECS = {'aac', 'mp4a', 'mp3', 'alac'}

# 지원하는 이미지 파일 확장자
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg',  # JPEG
    '.png',  # PNG
    '.gif',  # GIF
    '.bmp',  # Bitmap
    '.webp',  # WebP
    '.svg',  # SVG
    '.ico',  # Icon
    '.tiff', '.tif',  # TIFF
    '.heic', '.heif',  # HEIF (iOS)
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
    """모든 폴더에서 영상 파일 찾기 (하위 경로 포함)"""
    folders = get_video_folders()
    for folder in folders:
        try:
            # 파일명에 경로가 포함되어 있을 수 있음 (브라우징 모드)
            # 경로 구분자를 안전하게 처리
            if '/' in filename or '\\' in filename:
                # 하위 경로가 포함된 경우
                parts = filename.replace('\\', '/').split('/')
                current_path = folder['path']
                for part in parts:
                    current_path = os.path.join(current_path, part)

                # 경로 검증
                current_path_abs = os.path.abspath(current_path)
                base_path_abs = os.path.abspath(folder['path'])
                if current_path_abs.startswith(base_path_abs) and os.path.exists(current_path):
                    return current_path
            else:
                # 단순 파일명인 경우 기존 로직
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
        # 최신이면 바로 사용, pending 아님
        if not source_mtime or thumb_mtime >= source_mtime:
            return thumbnail_url, False

        # 오래된 경우 백그라운드 갱신을 예약하지만 UI에는 pending 표시 안 함
        schedule_thumbnail_generation(video_path, folder_path, filename, thumbnail_path, duration)
        return thumbnail_url, False

    scheduled = schedule_thumbnail_generation(video_path, folder_path, filename, thumbnail_path, duration)
    # 썸네일이 없는 경우에만 pending true 반환
    return (thumbnail_url if os.path.exists(thumbnail_path) else None), bool(scheduled)


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
                if is_hidden_file(file):
                    continue
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


def get_media_info(file_path, probe=True):
    """영상 코덱/해상도 등의 메타데이터 추출 (ffprobe 결과를 캐시).

    probe=False(목록 조회용 경량 모드): 캐시가 있으면 그 값을, 없으면 즉시 빈
    dict를 반환하고 백그라운드 큐에 ffprobe를 예약한다. 수백 개 파일을 동기
    ffprobe하느라 초기 로딩이 느려지는 것을 막기 위함. (캐시가 채워지면 다음
    조회부터 메타데이터가 표시됨)
    """
    try:
        mtime = os.path.getmtime(file_path)
    except OSError:
        return {}

    cache_entry = media_info_cache.get(file_path)
    if cache_entry and cache_entry['mtime'] == mtime:
        return cache_entry['data']

    if not probe:
        _enqueue_media_probe(file_path)
        return {}

    return _probe_media_info(file_path, mtime)


def _probe_media_info(file_path, mtime=None):
    """실제 ffprobe 수행 + 캐시 저장 (동기)."""
    if mtime is None:
        try:
            mtime = os.path.getmtime(file_path)
        except OSError:
            return {}

    # 워커가 이미 채웠을 수 있으니 재확인
    cache_entry = media_info_cache.get(file_path)
    if cache_entry and cache_entry['mtime'] == mtime:
        return cache_entry['data']

    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries',
        'format=bit_rate,duration:stream=index,codec_type,codec_name,codec_long_name,width,height,coded_width,coded_height,sample_aspect_ratio,bit_rate,duration,channels,channel_layout,side_data_list,tags',
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
        if not (duration and duration > 0):
            # 일부 컨테이너는 format에 duration이 없음 → 스트림에서 보강
            for s in streams:
                sd = parse_float(s.get('duration'))
                if sd and sd > 0:
                    duration = sd
                    break
        if duration and duration > 0:
            metadata['duration'] = duration

        video_stream = next((s for s in streams if s.get('codec_type') == 'video'), None)
        if video_stream:
            codec = video_stream.get('codec_name') or video_stream.get('codec_long_name')
            width = video_stream.get('width') or video_stream.get('coded_width')
            height = video_stream.get('height') or video_stream.get('coded_height')
            sar = video_stream.get('sample_aspect_ratio') or '1:1'
            rotation = 0
            try:
                rotation = int(video_stream.get('tags', {}).get('rotate', 0) or 0)
            except Exception:
                rotation = 0
            for side in video_stream.get('side_data_list') or []:
                if 'rotation' in side:
                    try:
                        rotation = int(side.get('rotation') or rotation)
                    except Exception:
                        pass

            display_width = width
            display_height = height

            # SAR 보정
            try:
                sar_num, sar_den = sar.split(':')
                sar_num = float(sar_num)
                sar_den = float(sar_den)
                if width and sar_num > 0 and sar_den > 0:
                    display_width = int(round(width * sar_num / sar_den))
            except Exception:
                pass

            if rotation % 180 != 0 and display_width and display_height:
                display_width, display_height = display_height, display_width

            resolution = f"{display_width}x{display_height}" if display_width and display_height else None
            if codec:
                codec_label = codec.upper() if len(codec) <= 6 else codec
                metadata['video_codec_info'] = f"{codec_label} ({resolution})" if resolution else codec_label
                metadata['video_codec'] = (video_stream.get('codec_name') or '').lower()
            if resolution:
                metadata['resolution'] = resolution

        audio_stream = next((s for s in streams if s.get('codec_type') == 'audio'), None)
        if audio_stream:
            codec = audio_stream.get('codec_name') or audio_stream.get('codec_long_name')
            channel_layout = audio_stream.get('channel_layout')
            channels = audio_stream.get('channels')
            channel_desc = channel_layout or (f"{channels}ch" if channels else None)
            if codec:
                codec_label = codec.upper() if len(codec) <= 6 else codec
                metadata['audio_codec_info'] = f"{codec_label} ({channel_desc})" if channel_desc else codec_label
                metadata['audio_codec'] = (audio_stream.get('codec_name') or '').lower()

        # 브라우저 호환성 판단 (코덱 + 컨테이너) → needs_transcode 플래그
        metadata.update(compute_transcode_requirement(file_path, metadata))

    except Exception:
        metadata = {}

    media_info_cache[file_path] = {'mtime': mtime, 'data': metadata}
    return metadata


# ── 메타데이터 백그라운드 워밍 (목록 조회를 막지 않도록) ───────────────────────
_media_probe_queue = queue.Queue()
_media_probe_inflight = set()
_media_probe_lock = threading.Lock()


def _enqueue_media_probe(file_path):
    """ffprobe를 백그라운드 워커에 예약 (중복 방지)."""
    with _media_probe_lock:
        if file_path in _media_probe_inflight:
            return
        _media_probe_inflight.add(file_path)
    _media_probe_queue.put(file_path)


def _media_probe_worker():
    while True:
        path = _media_probe_queue.get()
        try:
            _probe_media_info(path)
        except Exception:
            pass
        finally:
            with _media_probe_lock:
                _media_probe_inflight.discard(path)
            _media_probe_queue.task_done()


for _ in range(3):
    threading.Thread(target=_media_probe_worker, daemon=True).start()


def compute_transcode_requirement(file_path, metadata):
    """코덱/컨테이너를 검사해 브라우저 직접 재생 가능 여부와 트랜스코딩 필요성을 판단한다.

    반환: {'needs_transcode': bool, 'transcode_reason': str}
    - 비디오 코덱이 호환되지 않으면 재인코딩 필요
    - 오디오 코덱만 비호환이면 오디오만 재인코딩
    - 코덱은 호환되나 컨테이너만 비호환이면 리먹스(스트림 copy)만 필요 → 가장 빠름
    """
    ext = os.path.splitext(file_path)[1].lower()
    video_codec = (metadata.get('video_codec') or '').lower()
    audio_codec = (metadata.get('audio_codec') or '').lower()

    video_ok = (not video_codec) or video_codec in BROWSER_COMPATIBLE_VIDEO_CODECS
    audio_ok = (not audio_codec) or audio_codec in BROWSER_COMPATIBLE_AUDIO_CODECS
    container_ok = ext in BROWSER_COMPATIBLE_CONTAINERS

    reasons = []
    if not video_ok:
        reasons.append(f"video:{video_codec}")
    if not audio_ok:
        reasons.append(f"audio:{audio_codec}")
    if not container_ok:
        reasons.append(f"container:{ext}")

    return {
        'needs_transcode': not (video_ok and audio_ok and container_ok),
        'transcode_reason': ','.join(reasons),
        # 무소음 전체재생은 오디오를 사용하지 않으므로 오디오 코덱은 무시하고
        # 비디오 코덱/컨테이너만으로 트랜스코딩 필요 여부를 판단한다.
        'needs_transcode_silent': not (video_ok and container_ok),
    }


def is_hidden_file(name):
    """숨김 파일(.) 및 macOS AppleDouble(._) 동반 파일 여부.

    exFAT/FAT/네트워크 드라이브 등에 파일을 복사하면 macOS가 리소스 포크·확장속성을
    담은 '._파일명' 동반 파일을 만든다. 이건 실제 미디어가 아니라서 ffmpeg가 처리하지
    못하고(moov atom not found / Invalid data, exit 183) 썸네일·ffprobe가 실패한다.
    이름이 '.'로 시작하면 숨김/AppleDouble 모두 걸러진다.
    """
    return name.startswith('.')


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
            if is_hidden_file(file):
                continue
            file_path = os.path.join(folder_path, file)
            if os.path.isfile(file_path):
                ext = os.path.splitext(file)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    stat = os.stat(file_path)
                    media_info = get_media_info(file_path, probe=False)  # 경량: 캐시만, 동기 ffprobe 안 함
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


def get_history_positions(history_entries=None):
    """파일명 → {pos, watched} 매핑을 만든다 (재생중/완료/이어보기 표시용)."""
    positions = {}
    for entry in (history_entries if history_entries is not None else load_history()):
        fname = entry.get('filename')
        if not fname:
            continue
        try:
            pos = float(entry.get('position') or 0)
        except Exception:
            pos = 0
        positions[fname] = {'pos': pos, 'watched': bool(entry.get('watched'))}
    return positions


def apply_watch_status(item, history_positions):
    """목록 항목에 watched / resume_position 을 채운다 (히스토리는 파일명 기준)."""
    entry = history_positions.get(item.get('name')) or {}
    pos = entry.get('pos', 0)
    duration = item.get('duration') or 0
    watched = bool(entry.get('watched'))
    if not watched and duration and pos:
        threshold = max(duration * WATCH_COMPLETE_RATIO, duration - WATCH_COMPLETE_OFFSET)
        watched = pos >= threshold
    item['watched'] = watched
    item['resume_position'] = pos
    return item


@app.route('/')
def index():
    """메인 페이지"""
    return render_template('index.html')


def is_logged_in():
    return session.get('user') == LOGIN_USERNAME


@app.before_request
def require_login():
    """단순 세션 기반 접근 제어"""
    open_endpoints = {'login', 'logout', 'static'}
    if request.endpoint in open_endpoints:
        return

    # 허용: favicon 및 헬스체크 비슷한 경로
    if request.path.startswith('/static/') or request.path == '/favicon.ico':
        return

    if is_logged_in():
        return

    # API 요청은 401 반환, 페이지는 로그인 페이지로 리디렉션
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Unauthorized'}), 401
    return redirect(url_for('login', next=request.path))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    global failed_attempt_count, last_failed_ip, login_locked

    if login_locked:
        return render_template('login.html', locked=True, locked_ip=last_failed_ip, limit=FAILED_ATTEMPT_LIMIT)

    if request.method == 'POST':
        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''
        remember = request.form.get('remember') == 'on'

        if username == LOGIN_USERNAME and password == LOGIN_PASSWORD:
            session['user'] = username
            session.permanent = remember
            failed_attempt_count = 0
            last_failed_ip = request.remote_addr
            next_url = request.args.get('next') or url_for('index')
            return redirect(next_url)
        else:
            error = '아이디 또는 비밀번호가 올바르지 않습니다.'
            failed_attempt_count += 1
            last_failed_ip = request.remote_addr
            if failed_attempt_count >= FAILED_ATTEMPT_LIMIT:
                login_locked = True
                return render_template(
                    'login.html',
                    locked=True,
                    locked_ip=last_failed_ip,
                    limit=FAILED_ATTEMPT_LIMIT
                )

    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


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
                if is_hidden_file(file):
                    continue
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
    history_positions = get_history_positions()
    for video in videos:
        apply_watch_status(video, history_positions)
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


@app.route('/api/browse')
def browse_directory():
    """폴더 탐색 API - 현재 경로의 폴더와 파일(영상, 이미지) 반환"""
    # 현재 경로 파라미터
    folder_name = request.args.get('folder', '').strip()
    subpath = request.args.get('path', '').strip()

    # 폴더 정보 가져오기
    folders = get_video_folders()

    # 선택된 폴더 찾기
    selected_folder = None
    for folder_info in folders:
        if folder_info['name'] == folder_name:
            selected_folder = folder_info
            break

    if not selected_folder:
        return jsonify({'error': 'Folder not found'}), 404

    base_path = selected_folder['path']

    # 경로 조합 (보안: 상위 디렉토리 탐색 방지)
    if subpath:
        # ".." 제거하여 상위 디렉토리 공격 방지
        subpath_clean = os.path.normpath(subpath).replace('..', '')
        current_path = os.path.join(base_path, subpath_clean)
    else:
        current_path = base_path

    # 경로가 base_path 내부에 있는지 확인
    current_path_abs = os.path.abspath(current_path)
    base_path_abs = os.path.abspath(base_path)
    if not current_path_abs.startswith(base_path_abs):
        return jsonify({'error': 'Invalid path'}), 403

    if not os.path.exists(current_path) or not os.path.isdir(current_path):
        return jsonify({'error': 'Directory not found'}), 404

    # 현재 경로에서 폴더와 파일 목록 가져오기
    items = []
    history_positions = get_history_positions()

    try:
        for entry in os.listdir(current_path):
            entry_path = os.path.join(current_path, entry)

            # 숨김 파일 제외
            if entry.startswith('.'):
                continue

            if os.path.isdir(entry_path):
                # 폴더
                items.append({
                    'name': entry,
                    'type': 'folder',
                    'path': entry
                })
            elif os.path.isfile(entry_path):
                ext = os.path.splitext(entry)[1].lower()

                # 영상 파일
                if ext in VIDEO_EXTENSIONS:
                    stat = os.stat(entry_path)
                    media_info = get_media_info(entry_path, probe=False)  # 경량: 캐시만
                    thumbnail_url, thumbnail_pending = ensure_thumbnail_ready(
                        entry_path,
                        current_path,
                        entry,
                        stat.st_mtime,
                        media_info.get('duration') if media_info else None
                    )
                    items.append(apply_watch_status({
                        'name': entry,
                        'type': 'video',
                        'path': entry,
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        'extension': ext,
                        'thumbnail_url': thumbnail_url,
                        'thumbnail_pending': thumbnail_pending,
                        **media_info
                    }, history_positions))

                # 이미지 파일
                elif ext in IMAGE_EXTENSIONS:
                    stat = os.stat(entry_path)
                    items.append({
                        'name': entry,
                        'type': 'image',
                        'path': entry,
                        'size': stat.st_size,
                        'modified': datetime.fromtimestamp(stat.st_mtime).isoformat(),
                        'extension': ext
                    })

        # 정렬: 폴더 먼저, 그 다음 파일 (이름순)
        items.sort(key=lambda x: (x['type'] != 'folder', x['name'].lower()))

        # 상위 경로 정보
        parent_path = None
        if subpath:
            parent_path = os.path.dirname(subpath)

        return jsonify({
            'folder': folder_name,
            'current_path': subpath or '',
            'parent_path': parent_path,
            'items': items
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/image/<path:folder_name>/<path:filepath>')
def serve_image(folder_name, filepath):
    """이미지 파일 제공"""
    folders = get_video_folders()

    selected_folder = None
    for folder_info in folders:
        if folder_info['name'] == folder_name:
            selected_folder = folder_info
            break

    if not selected_folder:
        return jsonify({'error': 'Folder not found'}), 404

    base_path = selected_folder['path']

    # 보안: 상위 디렉토리 공격 방지
    filepath_clean = os.path.normpath(filepath).replace('..', '')
    image_path = os.path.join(base_path, filepath_clean)

    # 경로 검증
    image_path_abs = os.path.abspath(image_path)
    base_path_abs = os.path.abspath(base_path)
    if not image_path_abs.startswith(base_path_abs):
        return jsonify({'error': 'Invalid path'}), 403

    if not os.path.exists(image_path) or not os.path.isfile(image_path):
        return jsonify({'error': 'Image not found'}), 404

    # 확장자 확인
    ext = os.path.splitext(image_path)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        return jsonify({'error': 'Not an image file'}), 400

    # MIME 타입 결정
    mime_types_map = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.gif': 'image/gif',
        '.bmp': 'image/bmp',
        '.webp': 'image/webp',
        '.svg': 'image/svg+xml',
        '.ico': 'image/x-icon',
        '.tiff': 'image/tiff',
        '.tif': 'image/tiff',
        '.heic': 'image/heic',
        '.heif': 'image/heif',
    }
    mime_type = mime_types_map.get(ext, 'application/octet-stream')

    return send_file(image_path, mimetype=mime_type)


RANGE_CHUNK_SIZE = 1024 * 1024  # 1MB; stream in chunks instead of buffering whole range in RAM


def build_range_response(file_path, range_header, mimetype):
    """Range 요청을 청크 단위로 스트리밍하는 206 응답을 생성한다.

    전체 구간을 메모리에 읽지 않으므로(`bytes=0-` 같은 큰 요청도 안전) 대용량
    영상 탐색 시 메모리 사용량이 일정하게 유지된다.
    """
    size = os.path.getsize(file_path)
    byte_start, byte_end = 0, size - 1

    byte_range = range_header.replace('bytes=', '').split('-')
    if byte_range[0]:
        byte_start = int(byte_range[0])
    if len(byte_range) > 1 and byte_range[1]:
        byte_end = int(byte_range[1])

    byte_start = max(0, min(byte_start, size - 1))
    byte_end = max(byte_start, min(byte_end, size - 1))
    length = byte_end - byte_start + 1

    def generate():
        remaining = length
        with open(file_path, 'rb') as f:
            f.seek(byte_start)
            while remaining > 0:
                chunk = f.read(min(RANGE_CHUNK_SIZE, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    response = Response(
        stream_with_context(generate()),
        206,
        mimetype=mimetype,
        direct_passthrough=True
    )

    response.headers.add('Content-Range', f'bytes {byte_start}-{byte_end}/{size}')
    response.headers.add('Accept-Ranges', 'bytes')
    response.headers.add('Content-Length', str(length))

    return response


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

    return build_range_response(video_path, range_header, get_mime_type(filename))


@app.route('/api/video-silent/<path:filename>')
def serve_video_silent(filename):
    """오디오 없는 영상 스트리밍 (백그라운드 음악 보호용 - iOS 최적화)"""
    video_path = find_video_path(filename)

    if not video_path:
        return jsonify({'error': 'Video not found'}), 404

    # 캐시 디렉토리
    cache_dir = os.path.join(os.path.dirname(__file__), 'static', 'silent_videos')
    os.makedirs(cache_dir, exist_ok=True)

    # 캐시 파일명 생성
    name, ext = os.path.splitext(os.path.basename(filename))
    cache_filename = f"{name}_silent{ext}"
    cache_path = os.path.join(cache_dir, cache_filename)

    # 캐시 파일이 없거나 원본보다 오래된 경우 생성
    if not os.path.exists(cache_path) or os.path.getmtime(video_path) > os.path.getmtime(cache_path):
        try:
            # ffmpeg로 오디오 제거 (비디오 복사로 빠름)
            cmd = [
                'ffmpeg', '-y',
                '-i', video_path,
                '-an',  # 오디오 트랙 제거
                '-c:v', 'copy',  # 비디오는 복사 (재인코딩 없음)
                '-movflags', '+faststart',  # iOS 최적화
                cache_path
            ]
            subprocess.run(cmd, capture_output=True, check=True, timeout=300)
        except subprocess.CalledProcessError as e:
            return jsonify({'error': 'Failed to remove audio track'}), 500
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Audio removal timeout'}), 500

    # Flask의 send_file이 자동으로 Range request 처리 (iOS Safari 호환)
    return send_file(
        cache_path,
        mimetype=get_mime_type(filename),
        conditional=True  # Range request 자동 처리
    )


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


def build_full_transcode_command(video_path, cache_path, silent=False):
    """최대 호환성 전체 재인코딩 (폴백용) — iOS 호환 H.264 baseline + AAC.

    silent=True면 오디오를 완전히 제거(-an)한다 (무소음 전체재생 전용).
    """
    cmd = [
        'ffmpeg', '-y',
        '-i', video_path,
        '-map', '0:v:0?',
    ]
    if silent:
        cmd += ['-an']
    else:
        cmd += ['-map', '0:a:0?']
    cmd += [
        '-c:v', 'libx264',
        '-profile:v', 'baseline',
        '-level', '3.0',
        '-preset', 'fast',
        '-crf', '23',
        '-maxrate', '2M',
        '-bufsize', '4M',
        '-pix_fmt', 'yuv420p',
    ]
    if not silent:
        cmd += ['-c:a', 'aac', '-b:a', '128k', '-ar', '44100']
    cmd += ['-movflags', '+faststart', '-f', 'mp4', cache_path]
    return cmd


def build_fast_playback_command(video_path, cache_path, media_info, silent=False):
    """코덱 검사 결과를 바탕으로 가장 빠른 재생용 ffmpeg 명령을 만든다.

    - 비디오/오디오 코덱이 mp4에 그대로 담을 수 있으면 copy(재인코딩 없음 = 최속)
    - 비호환 스트림만 선택적으로 재인코딩
    - silent=True면 오디오를 아예 제거(-an)해 무소음 재생용으로 더 가볍게 만든다.
    """
    video_codec = (media_info.get('video_codec') or '').lower()
    audio_codec = (media_info.get('audio_codec') or '').lower()
    has_audio = bool(audio_codec)

    cmd = ['ffmpeg', '-y', '-i', video_path, '-map', '0:v:0?']
    if not silent:
        cmd += ['-map', '0:a:0?']

    # 비디오
    if video_codec in COPYABLE_VIDEO_CODECS:
        cmd += ['-c:v', 'copy']
        if video_codec in {'hevc', 'h265', 'hvc1'}:
            cmd += ['-tag:v', 'hvc1']  # Safari가 인식하도록 HEVC 태그 지정
    else:
        cmd += ['-c:v', 'libx264', '-profile:v', 'high', '-level', '4.1',
                '-preset', 'veryfast', '-crf', '23', '-pix_fmt', 'yuv420p']

    # 오디오 (무소음 모드는 트랙 제거)
    if silent or not has_audio:
        cmd += ['-an']
    elif audio_codec in COPYABLE_AUDIO_CODECS:
        cmd += ['-c:a', 'copy']
    else:
        cmd += ['-c:a', 'aac', '-b:a', '160k', '-ar', '48000']

    cmd += ['-movflags', '+faststart', '-f', 'mp4', cache_path]
    return cmd


@app.route('/api/transcode/<path:filename>')
def transcode_video(filename):
    """트랜스코딩 스트리밍 (iOS 호환 - 캐시 기반)

    쿼리 ?silent=1 → 무소음 전체재생 전용. 오디오를 제거(-an)해 더 가볍게
    인코딩하고, 별도의 silent_transcoded/ 캐시에 저장한다. 이 캐시는 마지막
    사용(접근) 시각 기준으로 짧은 보존시간이 지나면 자동 정리된다.
    """
    video_path = find_video_path(filename)

    if not video_path:
        return jsonify({'error': 'Video not found'}), 404

    silent = request.args.get('silent') in ('1', 'true', 'yes')

    # 트랜스코딩된 파일 캐시 경로 (무소음 전용은 별도 디렉토리)
    cache_subdir = 'silent_transcoded' if silent else 'transcoded'
    cache_dir = os.path.join(os.path.dirname(__file__), 'static', cache_subdir)
    os.makedirs(cache_dir, exist_ok=True)

    # 캐시 파일명 생성 (원본 파일명 기반)
    # 브라우징 모드에서는 filename에 하위 경로가 포함될 수 있다("east/이름.ts").
    # 경로 구분자를 치환해 캐시 디렉토리에 평탄한 단일 파일로 저장한다. (그대로 두면
    # 존재하지 않는 하위 디렉토리에 쓰려다 ffmpeg가 실패해 500이 발생)
    flat_name = os.path.splitext(filename)[0].replace('\\', '/').replace('/', '_')
    suffix = '_silent' if silent else '_transcoded'
    cache_filename = f"{flat_name}{suffix}.mp4"
    cache_path = os.path.join(cache_dir, cache_filename)

    # 캐시 파일이 없거나 원본보다 오래된 경우 트랜스코딩
    if not os.path.exists(cache_path) or os.path.getmtime(video_path) > os.path.getmtime(cache_path):
        # 코덱을 검사해 "가장 빠른" 방법 선택: 호환 스트림은 copy(리먹스), 비호환만 재인코딩
        media_info = get_media_info(video_path)
        ffmpeg_cmd = build_fast_playback_command(video_path, cache_path, media_info, silent=silent)

        try:
            subprocess.run(ffmpeg_cmd, capture_output=True, check=True, timeout=600)
        except subprocess.CalledProcessError as e:
            # copy(리먹스)가 실패하면 안전하게 전체 재인코딩으로 폴백
            fallback_cmd = build_full_transcode_command(video_path, cache_path, silent=silent)
            if ffmpeg_cmd != fallback_cmd:
                try:
                    subprocess.run(fallback_cmd, capture_output=True, check=True, timeout=600)
                except subprocess.CalledProcessError as e2:
                    return jsonify({
                        'error': 'Transcoding failed',
                        'details': e2.stderr.decode('utf-8') if e2.stderr else 'Unknown error'
                    }), 500
                except subprocess.TimeoutExpired:
                    return jsonify({'error': 'Transcoding timeout'}), 500
            else:
                return jsonify({
                    'error': 'Transcoding failed',
                    'details': e.stderr.decode('utf-8') if e.stderr else 'Unknown error'
                }), 500
        except subprocess.TimeoutExpired:
            return jsonify({'error': 'Transcoding timeout'}), 500

    # 마지막 사용 시각 갱신(atime)으로 미사용 자동삭제 기준을 정확히 유지.
    # mtime은 보존해 원본 변경 감지(신선도 검사)가 깨지지 않도록 한다.
    try:
        os.utime(cache_path, (time.time(), os.path.getmtime(cache_path)))
    except OSError:
        pass

    # Range request 지원으로 캐시 파일 제공
    range_header = request.headers.get('Range', None)

    if not range_header:
        return send_file(cache_path, mimetype='video/mp4')

    return build_range_response(cache_path, range_header, 'video/mp4')


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

        # 트랜스코딩된 캐시 파일도 삭제 (일반/무소음 전용 모두)
        for sub, suffix in (('transcoded', '_transcoded'), ('silent_transcoded', '_silent')):
            cache_path = os.path.join(
                os.path.dirname(__file__), 'static', sub, f"{name}{suffix}.mp4"
            )
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


@app.route('/api/delete-folder', methods=['DELETE'])
def delete_folder():
    """브라우징 모드에서 탐색된 하위 폴더 삭제"""
    data = request.get_json() or {}
    folder_name = data.get('folder_name')  # 루트 폴더명 (config에 설정된)
    subfolder_path = data.get('subfolder_path', '')  # 삭제할 하위 폴더 경로

    if not folder_name:
        return jsonify({'error': 'Folder name is required'}), 400

    # 루트 폴더 정보 찾기
    folders = get_video_folders()
    selected_folder = None
    for folder_info in folders:
        if folder_info['name'] == folder_name:
            selected_folder = folder_info
            break

    if not selected_folder:
        return jsonify({'error': 'Root folder not found'}), 404

    base_path = selected_folder['path']

    # 삭제할 폴더 경로 조합 (보안: 상위 디렉토리 탐색 방지)
    if subfolder_path:
        # ".." 제거하여 상위 디렉토리 공격 방지
        subfolder_clean = os.path.normpath(subfolder_path).replace('..', '')
        target_path = os.path.join(base_path, subfolder_clean)
    else:
        return jsonify({'error': 'Subfolder path is required'}), 400

    # 경로가 base_path 내부에 있는지 확인
    target_path_abs = os.path.abspath(target_path)
    base_path_abs = os.path.abspath(base_path)
    if not target_path_abs.startswith(base_path_abs):
        return jsonify({'error': 'Invalid path - security violation'}), 403

    # 루트 폴더 자체를 삭제하려는 시도 방지
    if target_path_abs == base_path_abs:
        return jsonify({'error': 'Cannot delete root folder'}), 403

    # 폴더가 존재하는지 확인
    if not os.path.exists(target_path):
        return jsonify({'error': 'Folder does not exist'}), 404

    if not os.path.isdir(target_path):
        return jsonify({'error': 'Path is not a directory'}), 400

    try:
        # 폴더 내 모든 비디오 파일의 썸네일 삭제 (재귀적으로)
        for root, dirs, files in os.walk(target_path):
            for file in files:
                file_path = os.path.join(root, file)
                ext = os.path.splitext(file)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    delete_thumbnail_for_video(file_path)

        # 폴더 삭제
        shutil.rmtree(target_path)

        return jsonify({'success': True, 'message': 'Folder deleted successfully'})
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


ORIENTATION_LANDSCAPE_DIR = 'horizontal'
ORIENTATION_PORTRAIT_DIR = 'vertical'


@app.route('/api/organize-orientation', methods=['POST'])
def organize_by_orientation():
    """현재 폴더의 영상을 방향(가로/세로)별 하위 폴더로 정리한다.

    - 세로형(높이 > 너비) → '세로형 영상' 폴더
    - 가로형 및 정사각형(너비 >= 높이) → '가로형 영상' 폴더
    - 대상 폴더가 없으면 생성. 회전/SAR 보정된 표시 해상도 기준으로 판정.
    """
    data = request.get_json(silent=True) or {}
    folder_name = (data.get('folder') or '').strip()
    subpath = (data.get('path') or '').strip()

    folders = get_video_folders()
    root = next((f for f in folders if f['name'] == folder_name), None)
    if not root:
        return jsonify({'error': 'Folder not found'}), 404

    base_path = root['path']
    if subpath:
        subpath_clean = os.path.normpath(subpath).replace('..', '')
        current_path = os.path.join(base_path, subpath_clean)
    else:
        current_path = base_path

    # 경로가 base_path 내부인지 확인
    if not os.path.abspath(current_path).startswith(os.path.abspath(base_path)):
        return jsonify({'error': 'Invalid path'}), 403
    if not os.path.isdir(current_path):
        return jsonify({'error': 'Directory not found'}), 404

    moved = {'landscape': 0, 'portrait': 0, 'skipped': 0}
    errors = []

    for entry in os.listdir(current_path):
        entry_path = os.path.join(current_path, entry)
        if entry.startswith('.') or not os.path.isfile(entry_path):
            continue
        if os.path.splitext(entry)[1].lower() not in VIDEO_EXTENSIONS:
            continue

        info = get_media_info(entry_path)
        resolution = info.get('resolution') or ''
        if 'x' not in resolution:
            moved['skipped'] += 1
            continue
        try:
            w_str, h_str = resolution.split('x')
            width, height = int(w_str), int(h_str)
        except (ValueError, TypeError):
            moved['skipped'] += 1
            continue
        if width <= 0 or height <= 0:
            moved['skipped'] += 1
            continue

        # 세로형: 높이 > 너비 / 그 외(가로·정사각형): 가로형
        if height > width:
            target_name, key = ORIENTATION_PORTRAIT_DIR, 'portrait'
        else:
            target_name, key = ORIENTATION_LANDSCAPE_DIR, 'landscape'

        target_dir = os.path.join(current_path, target_name)
        try:
            os.makedirs(target_dir, exist_ok=True)
            dest_path, _ = generate_unique_destination(target_dir, entry)
            shutil.move(entry_path, dest_path)
            delete_thumbnail_for_video(entry_path)  # 이전 위치 썸네일 정리
            moved[key] += 1
        except Exception as exc:
            errors.append({'file': entry, 'error': str(exc)})

    return jsonify({
        'success': True,
        'moved': moved,
        'errors': errors,
        'landscape_folder': ORIENTATION_LANDSCAPE_DIR,
        'portrait_folder': ORIENTATION_PORTRAIT_DIR
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
        silent_retention_hours = settings.get('silent_transcode_retention_hours', 24)

        base_static = os.path.join(os.path.dirname(__file__), 'static')
        silent_transcode_dir = os.path.join(base_static, 'silent_transcoded')

        cache_dirs = [
            os.path.join(base_static, 'transcoded'),
            os.path.join(base_static, 'hls'),
            os.path.join(base_static, 'silent_videos'),
            silent_transcode_dir
        ]

        now = time.time()
        max_age_seconds = max_age_days * 24 * 60 * 60
        silent_max_age_seconds = silent_retention_hours * 60 * 60
        deleted_count = 0
        freed_space = 0

        for cache_dir in cache_dirs:
            if not os.path.exists(cache_dir):
                continue

            # 무소음 트랜스코딩 캐시는 별도의 짧은 보존시간 적용
            dir_max_age_seconds = (
                silent_max_age_seconds
                if cache_dir == silent_transcode_dir
                else max_age_seconds
            )

            # 오래된 파일 삭제 (접근 시간 기준)
            for item in os.listdir(cache_dir):
                item_path = os.path.join(cache_dir, item)

                try:
                    # 파일 또는 디렉토리의 마지막 접근 시간
                    atime = os.path.getatime(item_path)
                    age_seconds = now - atime

                    if age_seconds > dir_max_age_seconds:
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
        # navigator.sendBeacon은 Content-Type이 다를 수 있으므로 강제 파싱
        data = request.get_json(force=True, silent=True) or {}
        history = load_history()

        filename = data.get('filename')
        if not filename:
            return jsonify({'error': 'Filename required'}), 400

        # 기존 기록 보존
        existing_entry = next((h for h in history if h.get('filename') == filename), None)

        # 중복 제거 (같은 영상이면 최신 기록만 유지)
        history = [h for h in history if h.get('filename') != filename]

        # 값 정규화
        try:
            position = float(data.get('position') or 0)
        except Exception:
            position = 0

        try:
            duration = float(data.get('duration') or 0)
        except Exception:
            duration = 0

        existing_pos = 0
        existing_watched = False
        if existing_entry:
            try:
                existing_pos = float(existing_entry.get('position') or 0)
            except Exception:
                existing_pos = 0
            existing_watched = bool(existing_entry.get('watched'))

        max_pos = max(existing_pos, position)
        watched_flag = existing_watched
        if duration <= 0 and existing_entry:
            try:
                duration = float(existing_entry.get('duration') or 0)
            except Exception:
                duration = 0

        if duration > 0:
            threshold = max(duration * WATCH_COMPLETE_RATIO, duration - WATCH_COMPLETE_OFFSET)
            if max_pos >= threshold:
                watched_flag = True

        # 새 기록 추가
        history.insert(0, {
            'filename': filename,
            'timestamp': datetime.now().isoformat(),
            'position': max_pos,
            'duration': duration,
            'watched': watched_flag
        })

        # 재생 위치 이어보기를 위해 충분히 큰 한도를 둔다 (라이브러리가 커도 위치 유지)
        history = history[:2000]

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


def get_edit_codec_args(width=None, height=None):
    """편집 시 사용할 비디오 코덱 인코딩 옵션 (맥은 GPU 활용)"""
    if platform.system() == 'Darwin':
        primary = [
            '-c:v', 'hevc_videotoolbox',
            '-tag:v', 'hvc1',
            '-b:v', '5M',
            '-maxrate', '6M',
            '-bufsize', '12M',
            '-pix_fmt', 'yuv420p',
        ]
        fallback = [
            '-c:v', 'libx265',
            '-preset', 'medium',
            '-crf', '22',
            '-pix_fmt', 'yuv420p',
        ]
        return primary, fallback

    primary = [
        '-c:v', 'libx265',
        '-preset', 'medium',
        '-crf', '22',
        '-pix_fmt', 'yuv420p',
    ]
    return primary, None


def probe_video_geometry(video_path):
    """영상의 width/height/rotation 정보를 ffprobe로 조회"""
    info = {'width': None, 'height': None, 'rotation': 0}
    probe_cmd = [
        'ffprobe', '-v', 'error',
        '-select_streams', 'v:0',
        '-show_entries', 'stream=width,height,side_data_list:stream_tags=rotate',
        '-of', 'json',
        video_path
    ]
    try:
        result = subprocess.run(probe_cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout or '{}')
        stream = (data.get('streams') or [{}])[0]
        info['width'] = stream.get('width')
        info['height'] = stream.get('height')
        rotate_tag = stream.get('tags', {}).get('rotate')

        rotation = 0
        try:
            rotation = int(rotate_tag)
        except Exception:
            rotation = 0

        # side_data_list displaymatrix 처리
        side_data = stream.get('side_data_list') or []
        for entry in side_data:
            if entry.get('rotation') is not None:
                try:
                    rotation = int(entry.get('rotation'))
                except Exception:
                    pass
        info['rotation'] = rotation
    except Exception:
        pass
    return info


def build_filter_args(geometry):
    """해상도/회전 정보를 기반으로 필터 체인 생성"""
    width = geometry.get('width')
    height = geometry.get('height')
    rotation = geometry.get('rotation', 0) or 0

    scale_w = f"trunc({int(width)}/2)*2" if width else "trunc(iw/2)*2"
    scale_h = f"trunc({int(height)}/2)*2" if height else "trunc(ih/2)*2"

    filters = [f"scale={scale_w}:{scale_h}"]

    normalized_rotation = rotation % 360
    if normalized_rotation in (90, 270):
        transpose_mode = 1 if normalized_rotation == 90 else 2
        filters.append(f"transpose={transpose_mode}")
    elif normalized_rotation == 180:
        filters.append("hflip,vflip")

    filters.append("setsar=1")

    return ['-vf', ','.join(filters), '-metadata:s:v:0', 'rotate=0']


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

        # 해상도/회전 조회 (원본 유지 목적)
        geometry = probe_video_geometry(video_path)
        codec_args, fallback_codec_args = get_edit_codec_args()
        filter_args = build_filter_args(geometry)

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

            cmd = base_cmd + codec_args + filter_args + [
                '-c:a', 'copy',
                '-movflags', '+faststart',
                temp_file
            ]

            try:
                subprocess.run(cmd, capture_output=True, check=True)
            except subprocess.CalledProcessError:
                if fallback_codec_args:
                    fallback_cmd = base_cmd + fallback_codec_args + filter_args + [
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


def process_video_extract(task_id, video_path, segments, output_dir):
    """선택한 구간을 각각 별도 파일로 추출"""
    try:
        edit_tasks[task_id]['status'] = 'processing'
        edit_tasks[task_id]['progress'] = 0
        edit_tasks[task_id]['outputs'] = []

        geometry = probe_video_geometry(video_path)
        codec_args, fallback_codec_args = get_edit_codec_args()
        filter_args = build_filter_args(geometry)
        base_name, _ = os.path.splitext(os.path.basename(video_path))
        valid_segments = [s for s in segments if s.get('end', 0) > s.get('start', 0)]
        total = len(valid_segments)

        for idx, segment in enumerate(valid_segments):
            start = float(segment['start'])
            end = float(segment['end'])
            duration = end - start
            if duration <= 0 or duration < MIN_SEGMENT_DURATION:
                continue

            timestamp = int(time.time())
            output_name = f"{base_name}_clip_{idx+1}_{timestamp}.mp4"
            output_path = os.path.join(output_dir, output_name)

            base_cmd = [
                'ffmpeg', '-y',
                '-ss', str(start),
                '-i', video_path,
                '-t', str(duration)
            ]

            cmd = base_cmd + codec_args + filter_args + [
                '-c:a', 'copy',
                '-movflags', '+faststart',
                output_path
            ]

            try:
                subprocess.run(cmd, capture_output=True, check=True)
            except subprocess.CalledProcessError:
                if fallback_codec_args:
                    fallback_cmd = base_cmd + fallback_codec_args + filter_args + [
                        '-c:a', 'copy',
                        '-movflags', '+faststart',
                        output_path
                    ]
                    subprocess.run(fallback_cmd, capture_output=True, check=True)
                else:
                    raise

            edit_tasks[task_id]['outputs'].append(output_name)
            if total > 0:
                edit_tasks[task_id]['progress'] = int(((idx + 1) / total) * 95)

        if not edit_tasks[task_id]['outputs']:
            raise ValueError("유효한 구간이 없습니다.")

        edit_tasks[task_id]['status'] = 'completed'
        edit_tasks[task_id]['progress'] = 100
    except Exception as e:
        edit_tasks[task_id]['status'] = 'error'
        edit_tasks[task_id]['error'] = str(e)


def process_video_split(task_id, video_path, segment_seconds):
    """영상을 일정 시간(분) 단위로 분할하고, 원본은 같은 경로의 'origin' 폴더로 이동한다.

    - 분할은 ffmpeg segment 머서 + 스트림 복사(-c copy)로 빠르고 무손실 처리한다.
      (복사 모드는 키프레임 경계에서 잘리므로 각 조각 길이는 요청값에 근사한다.)
    - 분할 조각은 원본이 있던 현재 경로에 그대로 저장한다.
    """
    try:
        edit_tasks[task_id]['status'] = 'processing'
        edit_tasks[task_id]['progress'] = 5
        edit_tasks[task_id]['outputs'] = []

        video_dir = os.path.dirname(video_path)
        original_name = os.path.basename(video_path)
        base_name, ext = os.path.splitext(original_name)

        # 충돌 방지를 위한 고유 토큰이 붙은 임시 출력 패턴
        token = str(int(time.time()))
        temp_prefix = f"{base_name}_part_{token}_"
        pattern = os.path.join(video_dir, f"{temp_prefix}%03d{ext}")

        cmd = [
            'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
            '-i', video_path,
            '-c', 'copy', '-map', '0',
            '-f', 'segment',
            '-segment_time', str(segment_seconds),
            '-reset_timestamps', '1',
            pattern
        ]
        subprocess.run(cmd, capture_output=True, check=True)

        # 생성된 조각들을 수집해 사람이 읽기 좋은 이름(_part001 …)으로 정리
        produced = sorted(
            f for f in os.listdir(video_dir)
            if f.startswith(temp_prefix) and f.endswith(ext)
        )
        if not produced:
            raise ValueError("분할 결과 파일이 생성되지 않았습니다.")

        edit_tasks[task_id]['progress'] = 80
        outputs = []
        for idx, fname in enumerate(produced, start=1):
            src = os.path.join(video_dir, fname)
            nice_name = f"{base_name}_part{idx:03d}{ext}"
            dst_path, dst_name = generate_unique_destination(video_dir, nice_name)
            os.rename(src, dst_path)
            outputs.append(dst_name)
        edit_tasks[task_id]['outputs'] = outputs

        # 원본을 같은 경로의 'origin' 폴더로 이동 (보존)
        origin_dir = os.path.join(video_dir, 'origin')
        os.makedirs(origin_dir, exist_ok=True)
        origin_path, _ = generate_unique_destination(origin_dir, original_name)
        shutil.move(video_path, origin_path)

        edit_tasks[task_id]['origin_folder'] = 'origin'
        edit_tasks[task_id]['status'] = 'completed'
        edit_tasks[task_id]['progress'] = 100
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode('utf-8', 'ignore') if isinstance(e.stderr, bytes) else (e.stderr or '')
        edit_tasks[task_id]['status'] = 'error'
        edit_tasks[task_id]['error'] = (err.strip()[-400:] or 'ffmpeg 분할 실패')
    except Exception as e:
        edit_tasks[task_id]['status'] = 'error'
        edit_tasks[task_id]['error'] = str(e)


@app.route('/api/split', methods=['POST'])
def start_split():
    """영상 분단위 분할 시작"""
    try:
        data = request.get_json() or {}
        filename = data.get('filename')

        if not filename:
            return jsonify({'error': 'Invalid parameters'}), 400

        # 분할 단위(분). 기본 2분, 0.1~120분 범위로 제한
        try:
            minutes = float(data.get('minutes', 2))
        except (TypeError, ValueError):
            minutes = 2
        minutes = max(0.1, min(minutes, 120))
        segment_seconds = round(minutes * 60, 3)

        video_path = find_video_path(filename)
        if not video_path:
            return jsonify({'error': 'Video not found'}), 404

        if not FFMPEG_AVAILABLE:
            return jsonify({'error': 'ffmpeg를 사용할 수 없습니다.'}), 500

        task_id = str(uuid.uuid4())
        edit_tasks[task_id] = {
            'status': 'pending',
            'progress': 0,
            'outputs': [],
            'error': None,
            'mode': 'split'
        }

        thread = threading.Thread(
            target=process_video_split,
            args=(task_id, video_path, segment_seconds)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'task_id': task_id,
            'message': 'Split task started'
        })

    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 500


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
            'error': None,
            'mode': 'delete'
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


@app.route('/api/extract', methods=['POST'])
def start_extract():
    """선택 구간 추출 시작"""
    try:
        data = request.get_json()
        filename = data.get('filename')
        segments = data.get('segments', [])

        if not filename or not segments:
            return jsonify({'error': 'Invalid parameters'}), 400

        video_path = find_video_path(filename)

        if not video_path:
            return jsonify({'error': 'Video not found'}), 404

        video_dir = os.path.dirname(video_path)

        task_id = str(uuid.uuid4())
        edit_tasks[task_id] = {
            'status': 'pending',
            'progress': 0,
            'outputs': [],
            'error': None,
            'mode': 'extract'
        }

        thread = threading.Thread(
            target=process_video_extract,
            args=(task_id, video_path, segments, video_dir)
        )
        thread.daemon = True
        thread.start()

        return jsonify({
            'task_id': task_id,
            'message': 'Extract task started'
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


import errno
from werkzeug.serving import WSGIRequestHandler

# 영상 플레이어가 탐색(seek)/중단으로 연결을 끊으면 macOS에서는 sendall이
# EPIPE/ECONNRESET 대신 EINVAL(errno 22)을 던지기도 한다. werkzeug는 EINVAL을
# 끊긴 연결로 인식하지 못해 트레이스백을 출력한다. 해당 errno들을
# BrokenPipeError(ConnectionError 하위 클래스)로 변환하면 werkzeug가 이를
# connection_dropped로 조용히 처리한다.
_DROPPED_CONN_ERRNOS = {
    errno.EPIPE,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ESHUTDOWN,
    errno.EINVAL,
    errno.EBADF,
}


class _QuietSocketWriter:
    """wfile 프록시: 끊긴 연결의 OSError를 BrokenPipeError로 변환한다.

    werkzeug의 run_wsgi는 ConnectionError/socket.timeout만 끊긴 연결로 처리하므로,
    소켓 쓰기 시점에 EINVAL 등을 ConnectionError 계열로 바꿔 줘야 트레이스백이
    출력되지 않는다.
    """

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def write(self, data):
        try:
            return self._wrapped.write(data)
        except OSError as exc:
            if exc.errno in _DROPPED_CONN_ERRNOS:
                raise BrokenPipeError(exc.errno, exc.strerror) from exc
            raise

    def flush(self):
        try:
            return self._wrapped.flush()
        except OSError as exc:
            if exc.errno in _DROPPED_CONN_ERRNOS:
                raise BrokenPipeError(exc.errno, exc.strerror) from exc
            raise

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


class QuietWSGIRequestHandler(WSGIRequestHandler):
    """끊긴 연결에서 발생하는 무해한 OSError 트레이스백을 억제한다."""

    def setup(self):
        super().setup()
        self.wfile = _QuietSocketWriter(self.wfile)


if __name__ == '__main__':
    try:
        print(f"🗑️  캐시 자동 정리: {cleanup_interval_hours}시간마다 실행 (최대 {settings['max_age_days']}일, {settings['max_size_gb']}GB)")
        app.run(
            debug=True,
            host='0.0.0.0',
            port=7777,
            threaded=True,
            request_handler=QuietWSGIRequestHandler,
        )
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
