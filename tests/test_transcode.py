"""The transcoding settings admins change on the main page, and what ffmpeg is asked for."""
import os
import subprocess
import time

import pytest

from conftest import signed_in
from reviewmeh import db, media

real_sleep = time.sleep
VID = 100


@pytest.fixture(autouse=True)
def fresh_settings():
    db.ex("DELETE FROM settings WHERE key='transcode'")
    yield
    db.ex("DELETE FROM settings WHERE key='transcode'")


def value_after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_starts_from_the_config(client):
    signed_in(client, 1)
    t = client.get('/api/transcode').json()
    assert (t['crf'], t['max_width']) == (18, 1920)
    assert {'crf': 18, 'name': 'High'} in t['qualities'] and 1920 in t['widths']


def test_ffmpeg_gets_what_was_saved(client):
    signed_in(client, 1)
    assert client.put('/api/transcode', json={'crf': 12, 'max_width': 3840}).status_code == 200
    assert client.get('/api/transcode').json()['crf'] == 12
    cmd = media.proxy_command('in.mov', 'out.mp4', 25)
    assert value_after(cmd, '-crf') == '12' and 'min(3840,iw)' in value_after(cmd, '-vf')


def test_default_command_is_unchanged(client):
    cmd = media.proxy_command('in.mov', 'out.mp4', 24)
    assert value_after(cmd, '-crf') == '18' and 'min(1920,iw)' in value_after(cmd, '-vf')
    assert value_after(cmd, '-preset') == 'fast' and value_after(cmd, '-g') == '10' and value_after(cmd, '-r') == '24'


@pytest.mark.parametrize('body', [{'crf': 1, 'max_width': 1920}, {'crf': 18, 'max_width': 99999}, {'crf': -5, 'max_width': 1920}])
def test_only_the_offered_choices_are_taken(client, body):
    signed_in(client, 1)
    assert client.put('/api/transcode', json=body).status_code == 400
    assert media.transcode_settings()['crf'] == 18


def test_admins_only(client):
    signed_in(client, 2)
    assert client.get('/api/transcode').status_code == 403
    assert client.put('/api/transcode', json={'crf': 12, 'max_width': 1920}).status_code == 403
    client.cookies.clear()
    assert client.get('/api/transcode').status_code == 403


def build(fake, client, movie, monkeypatch):
    monkeypatch.setattr(media.sg_bridge, 'download_movie', lambda vid, dest: (str(movie), 'movie.mp4', False))
    monkeypatch.setattr(media, 'enqueue', ENQUEUE)
    media.enqueue(VID, force=True)
    for _ in range(600):
        if not media._inflight:
            break
        real_sleep(.05)
    assert db.media(VID)['status'] == 'ready'
    return os.path.getsize(media.proxy_path(VID))


ENQUEUE = media.enqueue


def test_real_proxies_follow_the_quality(client, fake, movie, monkeypatch):
    db.ensure_media_row(VID)
    signed_in(client, 1)
    client.put('/api/transcode', json={'crf': 28, 'max_width': 1920})
    small = build(fake, client, movie, monkeypatch)
    client.put('/api/transcode', json={'crf': 12, 'max_width': 1920})
    large = build(fake, client, movie, monkeypatch)
    assert large > small


def test_pointers_go_to_the_room_only_while_someone_presents(client, fake):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100', 'project_id': '7'})
    token = db.q1('SELECT * FROM reviews')['token']
    signed_in(client, 1)
    with client.websocket_connect(f'/ws/{token}') as a, client.websocket_connect(f'/ws/{token}') as b:
        def until(ws, kind):
            while True:
                m = ws.receive_json()
                if m.get('type') == kind:
                    return m
        b.send_json({'type': 'pointer', 'p': [.1, .2], 'v': 100})   # nobody presenting: dropped
        a.send_json({'type': 'present', 'state': {}})
        until(b, 'present')
        b.send_json({'type': 'pointer', 'p': [.3, .4], 'v': 100})
        m = until(a, 'pointer')
        assert m['p'] == [.3, .4] and m['v'] == 100 and m['name']


def stored_movie(vid):
    db.ensure_media_row(vid)
    db.set_media(vid, status='ready', progress=1, built_at=1.0)
    os.makedirs(media.vdir(vid), exist_ok=True)
    open(media.proxy_path(vid), 'wb').write(b'x' * 100)


@pytest.fixture()
def two_movies(client, fake):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100', 'project_id': '7'})
    db.ex('DELETE FROM media')
    stored_movie(100)
    stored_movie(101)
    return db.q1('SELECT * FROM reviews')


def test_clearing_a_review_leaves_other_movies(client, two_movies):
    signed_in(client, 1)
    assert client.post(f"/api/r/{two_movies['token']}/clear-movies").json() == {'cleared': 1}
    assert db.media(100)['status'] == 'cleared' and not os.path.exists(media.proxy_path(100))
    assert db.media(101)['status'] == 'ready' and os.path.isfile(media.proxy_path(101))


def test_clearing_all(client, two_movies):
    signed_in(client, 1)
    assert client.get('/api/transcode').json()['stored'] == {'movies': 2, 'bytes': 200}
    assert client.post('/api/transcode/clear').json() == {'cleared': 2}
    assert not os.path.exists(media.proxy_path(101))
    assert client.get('/api/transcode').json()['stored'] == {'movies': 0, 'bytes': 0}


def test_clearing_keeps_notes(client, two_movies):
    item = db.q1('SELECT id FROM items')['id']
    signed_in(client, 1)
    token = two_movies['token']
    r = client.post(f'/api/r/{token}/notes', json={'item_id': item, 'frame': 3, 'text': 'keep me'})
    assert r.status_code == 200, r.text
    client.post('/api/transcode/clear')
    assert [n['text'] for n in client.get(f'/api/r/{token}').json()['notes']] == ['keep me']


def test_a_movie_being_made_is_left_alone(client, two_movies):
    media._inflight.add(100)
    try:
        signed_in(client, 1)
        assert client.post('/api/transcode/clear').json() == {'cleared': 1}
        assert db.media(100)['status'] == 'ready'
    finally:
        media._inflight.discard(100)


def test_clearing_is_for_admins(client, two_movies):
    signed_in(client, 2)
    assert client.post('/api/transcode/clear').status_code == 403
    assert client.post(f"/api/r/{two_movies['token']}/clear-movies").status_code == 403
    assert db.media(100)['status'] == 'ready'


def test_a_rebuilt_movie_has_a_new_stamp_on_the_page(client, fake, movie, monkeypatch):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100', 'project_id': '7'})
    db.ensure_media_row(VID)
    signed_in(client, 1)
    build(fake, client, movie, monkeypatch)
    first = db.media(VID)['built_at']
    real_sleep(.01)
    build(fake, client, movie, monkeypatch)
    second = db.media(VID)['built_at']
    assert second > first
    token = db.q1('SELECT token FROM reviews')['token']
    assert client.get(f'/api/r/{token}').json()['items'][0]['built_at'] == second


def test_status_is_for_admins(client, fake):
    signed_in(client, 2)
    assert client.get('/api/transcode/status').status_code == 403
    client.cookies.clear()
    assert client.get('/api/transcode/status').status_code == 403


def test_status_reports_the_machine(client, fake):
    signed_in(client, 1)
    s = client.get('/api/transcode/status').json()
    assert 0 <= s['cpu']['percent'] <= 100 and s['cpu']['cores'] >= 1
    assert 0 < s['memory']['used'] <= s['memory']['total'] and s['memory']['own'] > 0
    assert s['disk']['total'] > 0 and 0 <= s['disk']['percent'] <= 100 and s['disk']['free'] > 0
    assert s['workers'] == media.INGEST_WORKERS and s['people'] == 0


def test_status_lists_waiting_and_failed_movies(client, fake):
    from reviewmeh import system
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100,101', 'project_id': '7'})
    db.ex('DELETE FROM media')
    db.ensure_media_row(100)
    db.set_media(100, status='queued')
    db.ensure_media_row(101)
    db.set_media(101, status='error', error='ffmpeg said no')
    system._snapshot = (0, None)
    signed_in(client, 1)
    s = client.get('/api/transcode/status').json()
    assert s['waiting_count'] == 1 and s['waiting'][0]['code'] == 'sh010_anim_v001' and s['running'] == []
    assert s['failed_count'] == 1 and s['failed'][0]['error'] == 'ffmpeg said no'
    system._snapshot = (0, None)


def test_a_running_movie_shows_progress_and_time_left(client, fake):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100', 'project_id': '7'})
    db.ex('DELETE FROM media')
    db.ensure_media_row(100)
    db.set_media(100, status='transcoding', progress=.5)
    now = time.time()
    media._running[100] = {'started': now - 25, 'transcoding': now - 20, 'pid': None}
    try:
        task = media.activity()['running'][0]
    finally:
        media._running.pop(100)
    assert task['code'] == 'sh010_anim_v001' and task['progress'] == .5
    assert 24 < task['elapsed'] < 27 and 19 < task['left'] < 22


def test_finishing_leaves_nothing_running(client, fake, movie, monkeypatch):
    db.ensure_media_row(VID)
    build(fake, client, movie, monkeypatch)
    assert VID not in media._running
    a = media.activity()
    assert a['running'] == [] and a['ready'] >= 1 and a['finished'][0]['at'] == db.media(VID)['built_at']


def test_several_readings_share_one(client, fake):
    from reviewmeh import system
    system._snapshot = (0, None)
    assert system.snapshot() is system.snapshot()
    system._snapshot = (0, None)


def test_cpu_reads_true_from_any_thread(client, fake, monkeypatch):
    """Requests land on different worker threads: a last reading kept per thread would say 0 under load."""
    import collections
    import threading
    from reviewmeh import system
    Times = collections.namedtuple('Times', 'user idle')
    ticks = iter([Times(50, 50), Times(100, 100), Times(190, 110)])
    monkeypatch.setattr(system.psutil, 'cpu_times', lambda: next(ticks))
    monkeypatch.setattr(system, '_last_cpu', system._cpu_times())
    readings = []
    for _ in range(2):
        t = threading.Thread(target=lambda: readings.append(system._machine_cpu()))
        t.start()
        t.join()
    assert readings == [50.0, 90.0]


def test_a_short_process_reads_its_average_first_not_zero(client, fake):
    import subprocess as sp
    import psutil
    from reviewmeh import system
    burn = sp.Popen(['python3', '-c', 'import time\nend = time.time() + 1.2\nwhile time.time() < end: pass'])
    try:
        real_sleep(.6)
        cores, memory = system._cores(psutil.Process(burn.pid))
        assert cores > .2 and memory > 0
    finally:
        burn.wait()
        system._procs.pop(burn.pid, None)
