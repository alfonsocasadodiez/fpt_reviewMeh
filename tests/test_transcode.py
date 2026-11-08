"""The transcoding settings admins change on the main page, and what ffmpeg is asked for."""
import os
import shutil
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
    return ingest(monkeypatch)


def ingest(monkeypatch):
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


# ─── ShotGrid's MP4 instead of transcoding ────────────────────────────────────

def stream_hash(path):
    """A hash of the video as stored, so a copy matches and a re-encode doesn't."""
    return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:v', '-c', 'copy', '-f', 'streamhash', '-'],
                          capture_output=True, text=True, check=True).stdout


def use_shotgrid_mp4(client):
    signed_in(client, 1)
    assert client.put('/api/transcode', json={'crf': 18, 'max_width': 1920, 'source': 'shotgrid'}).status_code == 200


@pytest.fixture()
def encodes(monkeypatch):
    """The movies ffmpeg was asked to transcode."""
    made, real = [], media.proxy_command
    monkeypatch.setattr(media, 'proxy_command', lambda src, dest, fps: made.append(src) or real(src, dest, fps))
    return made


def sg_mp4(monkeypatch, path):
    monkeypatch.setattr(media.sg_bridge, 'download_sg_mp4', lambda vid, dest: shutil.copy(path, dest) and 'sh010_anim_v001.mp4')


def use_transcoding(client):
    signed_in(client, 1)
    assert client.put('/api/transcode', json={'crf': 18, 'max_width': 1920, 'source': 'transcode'}).status_code == 200


def test_new_installs_use_shotgrids_mp4_and_it_is_saved(client):
    signed_in(client, 1)
    t = client.get('/api/transcode').json()
    assert t['source'] == 'shotgrid' and [s['key'] for s in t['sources']] == ['shotgrid', 'transcode']
    use_transcoding(client)
    assert client.get('/api/transcode').json()['source'] == 'transcode'
    client.put('/api/transcode', json={'crf': 12, 'max_width': 1920})  # older pages leave it out: it stays
    assert media.transcode_settings() == {'crf': 12, 'max_width': 1920, 'source': 'transcode'}


@pytest.fixture()
def upgrade(client):
    """Start again on this database as if it came from before the movie source existed."""
    def start(saved=None):
        db.ex('DELETE FROM settings')
        if saved is not None:
            db.put_setting('transcode', saved)
        db.init()
        return media.transcode_settings()
    return start


def test_an_upgraded_install_keeps_transcoding(upgrade):
    assert upgrade()['source'] == 'transcode'
    assert upgrade({'crf': 12, 'max_width': 3840}) == {'crf': 12, 'max_width': 3840, 'source': 'transcode'}


def test_an_upgraded_install_keeps_a_source_already_chosen(upgrade, monkeypatch):
    assert upgrade({'crf': 18, 'max_width': 1920, 'source': 'shotgrid'})['source'] == 'shotgrid'
    monkeypatch.setattr(db, 'PROXY_SOURCE_SET', True)  # media.proxy_source in the config or environment
    assert upgrade()['source'] == 'shotgrid'


def test_a_new_install_stays_on_shotgrids_mp4_after_restarts(upgrade):
    upgrade()
    db.ex("DELETE FROM settings WHERE key='transcode'")  # as on a new install: checked once, nothing saved
    db.init()
    assert media.transcode_settings()['source'] == 'shotgrid'


def test_unknown_source_is_refused(client):
    signed_in(client, 1)
    assert client.put('/api/transcode', json={'crf': 18, 'max_width': 1920, 'source': 'ftp'}).status_code == 400
    assert media.transcode_settings()['source'] == 'shotgrid'


def test_shotgrids_mp4_is_used_as_it_is(client, fake, movie, monkeypatch, encodes):
    db.ensure_media_row(VID)
    use_shotgrid_mp4(client)
    sg_mp4(monkeypatch, movie)
    monkeypatch.setattr(media.sg_bridge, 'download_movie', lambda *a: pytest.fail('the uploaded movie was downloaded'))
    ingest(monkeypatch)
    m = db.media(VID)
    assert encodes == [] and stream_hash(media.proxy_path(VID)) == stream_hash(movie)
    assert (m['fps'], m['frame_count'], m['width'], m['height'], m['src_name']) == (24, 48, 320, 180, 'sh010_anim_v001.mp4')
    assert os.path.isfile(media.thumb_path(VID)) and sorted(os.listdir(media.vdir(VID))) == ['proxy.mp4', 'thumb.jpg']


def test_the_uploaded_movie_is_transcoded_without_an_mp4(client, fake, movie, monkeypatch, encodes):
    """FakeShotgrid's versions have no sg_uploaded_movie_mp4, as when ShotGrid hasn't made it yet."""
    db.ensure_media_row(VID)
    use_shotgrid_mp4(client)
    build(fake, client, movie, monkeypatch)
    assert encodes == [str(movie)] and db.media(VID)['src_name'] == 'movie.mp4'


def test_an_mp4_the_player_cant_step_through_is_transcoded(client, fake, movie, monkeypatch, encodes, tmp_path):
    odd = tmp_path / 'odd.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=1', '-c:v', 'libx264',
                    '-pix_fmt', 'yuv444p', str(odd)], check=True)
    db.ensure_media_row(VID)
    use_shotgrid_mp4(client)
    sg_mp4(monkeypatch, odd)
    build(fake, client, movie, monkeypatch)
    assert encodes == [str(movie)] and media.probe(media.proxy_path(VID))['streams'][0]['pix_fmt'] == 'yuv420p'


def test_a_failed_mp4_download_falls_back_to_transcoding(client, fake, movie, monkeypatch, encodes):
    def broken(vid, dest):
        open(dest, 'wb').write(b'half a movie')
        raise RuntimeError('connection reset')
    db.ensure_media_row(VID)
    use_shotgrid_mp4(client)
    monkeypatch.setattr(media.sg_bridge, 'download_sg_mp4', broken)
    build(fake, client, movie, monkeypatch)
    assert encodes == [str(movie)] and not os.path.exists(os.path.join(media.vdir(VID), 'sg.tmp.mp4'))


def test_the_transcode_setting_never_asks_for_the_mp4(client, fake, movie, monkeypatch, encodes):
    db.ensure_media_row(VID)
    use_transcoding(client)
    monkeypatch.setattr(media.sg_bridge, 'download_sg_mp4', lambda *a: pytest.fail('asked ShotGrid for its MP4'))
    build(fake, client, movie, monkeypatch)
    assert encodes == [str(movie)]


@pytest.mark.parametrize('stream, why', [
    ({'codec_name': 'hevc', 'pix_fmt': 'yuv420p', 'r_frame_rate': '24/1', 'avg_frame_rate': '24/1'}, 'not H.264'),
    ({'codec_name': 'h264', 'pix_fmt': 'yuv422p10le', 'r_frame_rate': '24/1', 'avg_frame_rate': '24/1'}, 'not 4:2:0'),
    ({'codec_name': 'h264', 'pix_fmt': 'yuv420p', 'r_frame_rate': '60/1', 'avg_frame_rate': '2997/125'}, 'not constant'),
])
def test_what_makes_an_mp4_unplayable(stream, why):
    assert why in media.unplayable({'streams': [{'codec_type': 'video', **stream}]})


def test_a_constant_rate_mp4_with_aac_is_playable():
    assert media.unplayable({'streams': [
        {'codec_type': 'video', 'codec_name': 'h264', 'pix_fmt': 'yuv420p', 'r_frame_rate': '24000/1001', 'avg_frame_rate': '24000/1001'},
        {'codec_type': 'audio', 'codec_name': 'aac'}]}) is None


def test_download_sg_mp4_takes_either_form_of_the_field(fake, monkeypatch, tmp_path):
    from reviewmeh import sg_bridge
    got = []
    conn = fake.as_user()
    for value in ('https://cdn.test/v.mp4', {'url': 'https://cdn.test/v.mp4', 'name': 'x'}):
        monkeypatch.setattr(conn, 'find_one', lambda *a, v=value, **kw: {'id': VID, 'sg_uploaded_movie': {'name': 'sh010.mov'}, 'sg_uploaded_movie_mp4': v})
        monkeypatch.setattr(conn, 'download_attachment', lambda att, file_path: got.append(att['url']), raising=False)
        assert sg_bridge.download_sg_mp4(VID, str(tmp_path / 'v.mp4')) == 'sh010.mp4'
    assert got == ['https://cdn.test/v.mp4'] * 2
    monkeypatch.setattr(conn, 'find_one', lambda *a, **kw: {'id': VID, 'sg_uploaded_movie_mp4': None})
    assert sg_bridge.download_sg_mp4(VID, str(tmp_path / 'v.mp4')) is None
