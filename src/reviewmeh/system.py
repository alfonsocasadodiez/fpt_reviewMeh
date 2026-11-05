#!/usr/bin/env python3
"""What the machine and ReviewMeh are doing right now, for admins on the main page's Transcoding tab."""
import os
import shutil
import threading
import time

import psutil

from . import media
from .config import INGEST_WORKERS, MEDIA_ROOT

SNAPSHOT_SECONDS = 1     # several open tabs share one reading: CPU figures need time between calls
STORED_SECONDS = 20      # adding up the movies on disk walks every folder

_lock = threading.Lock()
_snapshot = (0, None)
_stored = (0, None)
_procs = {}              # pid -> Process: CPU use is measured between calls on the same object


def _cpu_times():
    t = psutil.cpu_times()
    idle = t.idle + getattr(t, 'iowait', 0)
    return sum(t) - idle, sum(t)


_last_cpu = _cpu_times()  # the first reading counts from here


def _machine_cpu():
    """Percent of the machine busy since the last reading. Worked out here because psutil.cpu_percent() remembers
    its last reading per thread, and every request can land on a different one, which would read 0."""
    global _last_cpu
    busy, total = _cpu_times()
    was_busy, was_total = _last_cpu
    _last_cpu = (busy, total)
    return round(max(0, min(100, (busy - was_busy) / (total - was_total) * 100)), 1) if total > was_total else 0.0


def _cores(proc):
    """Cores this process has used since the last reading, and its memory. None when it's gone."""
    known = _procs.get(proc.pid)
    first = known != proc  # new, or its pid has been reused
    if first:
        known = _procs[proc.pid] = proc
    try:
        cores = known.cpu_percent(None) / 100
        if first:  # psutil's first reading is always 0, and a short ffmpeg may only be seen once: use its average so far
            t = known.cpu_times()
            cores = (t.user + t.system) / max(.1, time.time() - known.create_time())
        return cores, known.memory_info().rss
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        _procs.pop(proc.pid, None)
        return None


def _own_usage():
    """ReviewMeh and everything it started, ffmpeg included. → (cores, memory bytes, {pid: cores})"""
    me = psutil.Process()
    try:
        children = me.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    cores, memory, by_pid = 0.0, 0, {}
    for proc in [me] + children:
        got = _cores(proc)
        if got:
            cores += got[0]
            memory += got[1]
            by_pid[proc.pid] = got[0]
    for pid in set(_procs) - set(by_pid):
        del _procs[pid]
    return cores, memory, by_pid


def _stored_movies():
    global _stored
    if time.time() - _stored[0] > STORED_SECONDS:
        _stored = (time.time(), media.stored())
    return _stored[1]


def _read():
    cores, memory, by_pid = _own_usage()
    vm = psutil.virtual_memory()
    os.makedirs(MEDIA_ROOT, exist_ok=True)
    du = shutil.disk_usage(MEDIA_ROOT)
    activity = media.activity()
    for task in activity['running']:
        task['cores'] = by_pid.get(task.pop('pid'))
    return {
        'at': time.time(),
        'in_container': os.path.exists('/.dockerenv'),
        'cpu': {'percent': _machine_cpu(), 'cores': psutil.cpu_count() or 1, 'load': psutil.getloadavg()[0], 'own_cores': cores},
        'memory': {'percent': vm.percent, 'used': vm.total - vm.available, 'total': vm.total, 'own': memory},
        'disk': {'percent': round(du.used / du.total * 100, 1) if du.total else 0, 'used': du.used, 'total': du.total, 'free': du.free,
                 'movies': _stored_movies()},
        'workers': INGEST_WORKERS,
        **activity,
    }


def snapshot():
    global _snapshot
    with _lock:
        if time.time() - _snapshot[0] >= SNAPSHOT_SECONDS or _snapshot[1] is None:
            _snapshot = (time.time(), _read())
        return _snapshot[1]
