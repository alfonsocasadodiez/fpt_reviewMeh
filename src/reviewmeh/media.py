#!/usr/bin/env python3
"""
Media ingest: pull a Version's movie from ShotGrid, transcode a frame-accurate review proxy
(constant fps, short GOP, no B-frames, faststart), probe it, make a thumbnail.
Proxies are keyed by version id, so a version is ingested once and can be streamed by any review
(its items, mini-cut / cut neighbours, compare versions).

Proxies are a cache of ShotGrid's movies: clear_idle() removes the ones nobody has used for a while, and they're
rebuilt from ShotGrid when needed again. Notes and their drawings live in the database and don't depend on them.
"""
import os
import json
import time
import shutil
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from . import db
from . import sg_bridge
from .config import MEDIA_ROOT, PROXY_MAX_WIDTH, PROXY_CRF, INGEST_WORKERS

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=INGEST_WORKERS, thread_name_prefix='ingest')
_inflight = set()
_running = {}  # version_id -> {'started', 'transcoding', 'pid'}: what the status panel shows
_lock = threading.Lock()
_notify = None  # callable(version_id) set by the server to push status over websockets


def set_notifier(fn):
    global _notify
    _notify = fn


def vdir(version_id):
    return os.path.join(MEDIA_ROOT, str(version_id))


QUALITIES = [{'crf': 28, 'name': 'Draft'}, {'crf': 23, 'name': 'Standard'}, {'crf': 18, 'name': 'High'}, {'crf': 12, 'name': 'Highest'}]
MAX_WIDTHS = [1280, 1920, 2560, 3840]


def transcode_settings():
    """What new proxies are made with: as admins set it on the main page, else the config."""
    return {'crf': PROXY_CRF, 'max_width': PROXY_MAX_WIDTH, **db.get_setting('transcode', {})}


def proxy_command(src, dest, fps):
    s = transcode_settings()
    return ['ffmpeg', '-y', '-nostdin', '-v', 'error', '-i', src,
            '-map', '0:v:0', '-map', '0:a:0?',
            '-vf', f"scale='trunc(min({s['max_width']},iw)/2)*2':-2:flags=lanczos,format=yuv420p",
            '-fps_mode', 'cfr', '-r', str(fps),
            '-c:v', 'libx264', '-preset', 'fast', '-crf', str(s['crf']), '-g', '10', '-bf', '0',
            '-c:a', 'aac', '-b:a', '160k',
            '-movflags', '+faststart', '-progress', 'pipe:1', '-nostats', dest]


def proxy_path(version_id):
    return os.path.join(vdir(version_id), 'proxy.mp4')


def thumb_path(version_id):
    return os.path.join(vdir(version_id), 'thumb.jpg')


def _update(version_id, **kw):
    db.set_media(version_id, **kw)
    if _notify:
        try:
            _notify(version_id)
        except Exception:
            logger.exception('notify failed')


def enqueue(version_id, force=False):
    db.touch_media(version_id)
    m = db.media(version_id)
    if m and m['status'] == 'ready' and os.path.isfile(proxy_path(version_id)) and not force:
        return
    with _lock:
        if version_id in _inflight:
            return
        _inflight.add(version_id)
    _update(version_id, status='queued', progress=0, error=None)
    _executor.submit(_ingest, version_id)


def ensure(version_ids):
    """Queue ingest (in the given order) for versions without a ready proxy; returns their media rows."""
    for vid in version_ids:
        db.ensure_media_row(vid)
        if (db.media(vid) or {}).get('status') not in ('error', 'nomedia'):  # failures retry via reingest only
            enqueue(vid)
    return db.media_many(version_ids)


def wait_ready(version_ids, timeout=1800):
    """For work that needs the frames, such as exports and publishing: start whatever is missing and wait for it."""
    ids = set(version_ids)
    ensure(ids)
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _lock:
            if not _inflight & ids:
                return
        time.sleep(1)


def resume_pending():
    for m in db.q("SELECT version_id FROM media WHERE status NOT IN ('ready','error','nomedia','cleared')"):
        enqueue(m['version_id'], force=True)


def clear_idle(cutoff, keep=()):
    """Remove the files of versions unused since `cutoff`, except those in `keep`. Returns how many were removed."""
    cleared = []
    for m in db.q("SELECT version_id FROM media WHERE last_used<? AND status IN ('ready','error','nomedia')", (cutoff,)):
        vid = m['version_id']
        if vid in keep:
            continue
        with _lock:  # enqueue() can't start this version while its folder is going
            now = db.media(vid)
            if vid in _inflight or now['last_used'] >= cutoff:
                continue
            if now['status'] == 'ready':
                db.set_media(vid, status='cleared', progress=0)
                cleared.append(vid)
            remove_media(vid)
    for vid in cleared:
        _update(vid)  # pages still open on it hear about it
    return len(cleared)


def clear(version_ids=None):
    """Remove finished proxies, of these versions or of all. They're made again when next opened. Returns how many went."""
    wanted = None if version_ids is None else set(version_ids)
    cleared = []
    for m in db.q("SELECT version_id FROM media WHERE status='ready'"):
        vid = m['version_id']
        if wanted is not None and vid not in wanted:
            continue
        with _lock:  # enqueue() can't start this version while its folder is going
            if vid in _inflight:
                continue
            db.set_media(vid, status='cleared', progress=0)
            remove_media(vid)
            cleared.append(vid)
    for vid in cleared:
        _update(vid)  # pages still open on it hear about it
    return len(cleared)


def stored():
    """How many finished proxies there are and the disk they take, with their thumbnails."""
    rows = db.q("SELECT version_id FROM media WHERE status='ready'")
    size = 0
    for m in rows:
        for root, _, files in os.walk(vdir(m['version_id'])):
            size += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    return {'movies': len(rows), 'bytes': size}


def activity():
    """Movies being made and waiting, how many there are of each state, and the latest that finished or failed."""
    code = '(SELECT i.version_code FROM items i WHERE i.version_id=m.version_id LIMIT 1) AS version_code'
    now = time.time()
    running, waiting = [], []
    for m in db.q(f"""SELECT m.version_id, m.status, m.progress, {code} FROM media m
                      WHERE m.status NOT IN ('ready','error','nomedia','cleared') ORDER BY m.updated_at"""):
        run = _running.get(m['version_id'])
        if not run or m['status'] == 'queued':
            waiting.append({'version_id': m['version_id'], 'code': m['version_code']})
            continue
        since = now - run['transcoding'] if run['transcoding'] else None
        running.append({'version_id': m['version_id'], 'code': m['version_code'], 'status': m['status'], 'progress': m['progress'],
                        'elapsed': now - run['started'], 'pid': run['pid'],
                        'left': since * (1 - m['progress']) / m['progress'] if since and m['progress'] >= .05 else None})
    counts = {r['status']: r['n'] for r in db.q('SELECT status, COUNT(*) AS n FROM media GROUP BY status')}
    return {'running': running, 'waiting': waiting[:8], 'waiting_count': len(waiting),
            'ready': counts.get('ready', 0), 'failed_count': counts.get('error', 0),
            'failed': [{'code': f['version_code'], 'error': f['error'], 'at': f['updated_at']} for f in db.q(
                f"SELECT m.error, m.updated_at, {code} FROM media m WHERE m.status='error' ORDER BY m.updated_at DESC LIMIT 5")],
            'finished': [{'code': f['version_code'], 'at': f['built_at']} for f in db.q(
                f"SELECT m.built_at, {code} FROM media m WHERE m.status='ready' AND m.built_at IS NOT NULL ORDER BY m.built_at DESC LIMIT 5")]}


def probe(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _fps(stream):
    for key in ('r_frame_rate', 'avg_frame_rate'):
        val = stream.get(key) or '0/0'
        n, _, d = val.partition('/')
        try:
            f = float(n) / float(d or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if 1 <= f <= 240:
            return round(f, 3)
    return 24.0


def _ingest(version_id):
    workdir = vdir(version_id)
    _running[version_id] = {'started': time.time(), 'transcoding': None, 'pid': None}
    try:
        os.makedirs(workdir, exist_ok=True)
        _update(version_id, status='downloading', progress=0)
        src, src_name, is_temp = sg_bridge.download_movie(version_id, workdir)
        if not src:
            _update(version_id, status='nomedia', error='Version has no uploaded movie')
            return

        info = probe(src)
        vstreams = [s for s in info['streams'] if s.get('codec_type') == 'video']
        if not vstreams:
            raise RuntimeError('No video stream in source')
        vs = vstreams[0]
        fps = _fps(vs)
        has_audio = any(s.get('codec_type') == 'audio' for s in info['streams'])
        duration = float(info.get('format', {}).get('duration') or vs.get('duration') or 0)
        total_frames = max(1, round(duration * fps)) if duration else 0

        _update(version_id, status='transcoding', progress=0, src_name=src_name, fps=fps, has_audio=int(has_audio))
        tmp = os.path.join(workdir, 'proxy.tmp.mp4')
        proc = subprocess.Popen(proxy_command(src, tmp, fps), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        _running[version_id].update(transcoding=time.time(), pid=proc.pid)
        last = 0
        for line in proc.stdout:
            if line.startswith('frame=') and total_frames:
                pct = min(0.99, int(line.split('=')[1] or 0) / total_frames)
                if pct - last >= 0.05:
                    last = pct
                    _update(version_id, progress=pct)
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(f'ffmpeg failed: {err[-500:]}')
        os.replace(tmp, proxy_path(version_id))

        # Exact frame count / size from the proxy itself
        out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets',
                              '-show_entries', 'stream=nb_read_packets,width,height', '-of', 'json',
                              proxy_path(version_id)], capture_output=True, text=True, check=True).stdout
        st = json.loads(out)['streams'][0]
        frames = int(st.get('nb_read_packets') or total_frames or 1)

        mid = max(0.0, (frames / 2) / fps)
        subprocess.run(['ffmpeg', '-y', '-nostdin', '-v', 'error', '-ss', f'{mid:.3f}', '-i', proxy_path(version_id),
                        '-frames:v', '1', '-vf', 'scale=320:-2', '-q:v', '4', thumb_path(version_id)], check=False)

        if is_temp:
            os.remove(src)
        _update(version_id, status='ready', progress=1, error=None, fps=fps, frame_count=frames,
                width=int(st['width']), height=int(st['height']), last_used=time.time(), built_at=time.time())
        logger.info('Ingested version %s: %s frames @ %s fps', version_id, frames, fps)
    except Exception as e:
        logger.exception('Ingest failed for version %s', version_id)
        _update(version_id, status='error', error=str(e)[:500])
    finally:
        for f in ('proxy.tmp.mp4',):
            p = os.path.join(workdir, f)
            if os.path.exists(p):
                os.remove(p)
        with _lock:
            _inflight.discard(version_id)
        _running.pop(version_id, None)


def extract_frame(version_id, index, dest):
    """Write frame `index` (0-based) of the proxy as PNG. Proxies are constant fps with short GOPs, so seeking to just
    before the frame only decodes a few; select=eq(n) (decodes from the start, slow deep in a shot) is the fallback."""
    index = int(index)
    db.touch_media(version_id)
    proxy, fps = proxy_path(version_id), (db.media(version_id) or {}).get('fps')
    base = ['ffmpeg', '-y', '-nostdin', '-v', 'error']
    if os.path.exists(dest):
        os.remove(dest)
    if fps:
        subprocess.run(base + ['-ss', f'{max(0, index - 0.25) / fps:.6f}', '-i', proxy, '-frames:v', '1', '-compression_level', '1', dest], check=True)
    if not fps or not os.path.isfile(dest) or not os.path.getsize(dest):
        subprocess.run(base + ['-i', proxy, '-vf', f'select=eq(n\\,{index})', '-fps_mode', 'passthrough', '-frames:v', '1', dest], check=True)
    if not os.path.isfile(dest):
        raise RuntimeError(f'Frame {index} not found')
    return dest


def remove_media(version_id):
    shutil.rmtree(vdir(version_id), ignore_errors=True)
