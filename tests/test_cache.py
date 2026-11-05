"""The cleanup removes what ShotGrid can give back. Notes and their drawings stay, and can be edited once the proxy is rebuilt."""
import hashlib
import hmac
import io
import json
import os
import shutil
import time
import zipfile

import pytest
from PIL import Image, ImageChops, ImageStat

from conftest import signed_in
from reviewmeh import db, media
from reviewmeh.media import enqueue as real_enqueue

real_sleep = time.sleep
SHAPE = [{'t': 'rect', 'c': '#ff0000', 'w': .02, 'a': [.2, .2], 'b': [.6, .6]}]
VID = 100
LONG_AGO = time.time() - 16 * 86400


def wait_for_ingest():
    for _ in range(600):
        if not media._inflight:
            return
        real_sleep(.05)
    raise AssertionError('ingest did not finish')


@pytest.fixture()
def built(fake, movie, monkeypatch):
    """Real proxies, built by the real ingest from a movie ShotGrid would serve."""
    shutil.rmtree(media.vdir(VID), ignore_errors=True)
    monkeypatch.setattr(media, 'enqueue', real_enqueue)
    monkeypatch.setattr(media.sg_bridge, 'download_movie', lambda vid, dest: (str(movie), 'movie.mp4', False))


@pytest.fixture()
def review(client, built):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': str(VID), 'project_id': '7'})
    wait_for_ingest()
    assert db.media(VID)['status'] == 'ready'
    signed_in(client, 1, login='ada@test', name='Ada')
    r = db.q1('SELECT * FROM reviews')
    r['item'] = db.q1('SELECT id FROM items')['id']
    r['note'] = client.post(f"/api/r/{r['token']}/notes", json={'item_id': r['item'], 'frame': 5, 'text': 'look here', 'shapes': SHAPE}).json()
    return r


def age(version_id=VID):
    db.ex('UPDATE media SET last_used=? WHERE version_id=?', (LONG_AGO, version_id))


def test_unused_proxy_is_deleted_and_the_drawing_is_not(client, review):
    upload = client.post(f"/api/r/{review['token']}/files?name=ref.pdf", content=b'%PDF').json()
    attached = client.post(f"/api/r/{review['token']}/notes", json={'item_id': review['item'], 'frame': 7, 'text': 'see file', 'file_ids': [upload['id']]}).json()
    assert client.put(f"/api/r/{review['token']}/drafts", json={'data': {str(review['item']): {'5': SHAPE}}, 'updated_at': time.time()}).status_code == 200
    before = db.q1('SELECT shapes, frame, text FROM notes WHERE id=?', (review['note']['id'],))
    draft = db.q('SELECT data FROM drafts')
    assert draft
    age()

    client.server.clear_cache(set())

    assert db.media(VID)['status'] == 'cleared'
    assert not os.path.exists(media.vdir(VID))
    assert db.q1('SELECT shapes, frame, text FROM notes WHERE id=?', (review['note']['id'],)) == before
    assert db.q('SELECT data FROM drafts') == draft
    files = db.note_files(attached['id'])
    assert [f['name'] for f in files] == ['ref.pdf']
    assert os.path.isfile(db.file_row(files[0]['id'])['path'])


def test_opening_the_review_rebuilds_and_the_drawing_can_be_edited(client, review, tmp_path):
    token, nid = review['token'], review['note']['id']
    url = f"/api/r/{token}/frame/{review['item']}/5.png?note={nid}"
    first = Image.open(io.BytesIO(client.get(url).content)).convert('RGB')
    frames = db.media(VID)['frame_count']
    age()
    client.server.clear_cache(set())
    assert client.get(url).status_code == 409

    opened = client.get(f'/api/r/{token}').json()
    assert [n['shapes'] for n in opened['notes']] == [SHAPE]
    wait_for_ingest()

    m = db.media(VID)
    assert m['status'] == 'ready' and m['frame_count'] == frames and os.path.isfile(media.proxy_path(VID))
    again = Image.open(io.BytesIO(client.get(url).content)).convert('RGB')
    assert max(ImageStat.Stat(ImageChops.difference(first, again)).mean) < 3  # the drawing lands on the same picture

    moved = [{**SHAPE[0], 'b': [.8, .8]}]
    edited = client.patch(f'/api/r/{token}/notes/{nid}', json={'shapes': moved, 'text': 'look here instead'})
    assert edited.status_code == 200, edited.text
    assert edited.json()['shapes'] == moved
    changed = Image.open(io.BytesIO(client.get(url).content)).convert('RGB')
    assert ImageChops.difference(again, changed).getbbox() is not None


def test_recent_and_open_versions_stay(client, review):
    client.server.clear_cache(set())
    assert db.media(VID)['status'] == 'ready'

    age()
    client.server.clear_cache({VID})
    assert db.media(VID)['status'] == 'ready' and os.path.isfile(media.proxy_path(VID))


def test_watching_counts_as_use(client, review):
    age()
    assert client.get(f"/api/r/{review['token']}/media/{VID}/proxy.mp4").status_code == 200
    client.server.clear_cache(set())
    assert db.media(VID)['status'] == 'ready'


def test_a_version_being_built_is_left_alone(client, review):
    age()
    media._inflight.add(VID)
    try:
        client.server.clear_cache(set())
    finally:
        media._inflight.discard(VID)
    assert db.media(VID)['status'] == 'ready' and os.path.isfile(media.proxy_path(VID))


def test_failed_versions_lose_leftovers_and_keep_their_status(client, review):
    db.set_media(VID, status='error', error='ffmpeg failed')
    age()
    client.server.clear_cache(set())
    m = db.media(VID)
    assert m['status'] == 'error' and m['error'] == 'ffmpeg failed'
    assert not os.path.exists(media.vdir(VID))


def test_zero_days_keeps_everything(client, review, monkeypatch):
    monkeypatch.setattr(client.server, 'CACHE_DAYS', 0)
    age()
    client.server.clear_cache(set())
    assert db.media(VID)['status'] == 'ready'


def test_copies_of_shotgrids_files_expire_and_uploads_do_not(client, monkeypatch):
    server = client.server
    old = time.time() - 16 * 86400
    upload_dir = os.path.join(server.UPLOAD_DIR, 'kept')
    for folder in (server.ATTACH_DIR, server.AVATAR_DIR, upload_dir):
        os.makedirs(folder, exist_ok=True)
    stale, fresh, upload = os.path.join(server.ATTACH_DIR, '1.img'), os.path.join(server.AVATAR_DIR, '2.img'), os.path.join(upload_dir, 'a.pdf')
    for path in (stale, fresh, upload):
        open(path, 'wb').close()
    os.utime(stale, (old, old))
    os.utime(upload, (old, old))

    server.clear_cache(set())

    assert not os.path.exists(stale)
    assert os.path.exists(fresh) and os.path.exists(upload)


def test_restarting_does_not_rebuild_cleared_proxies(client, review, monkeypatch):
    age()
    client.server.clear_cache(set())
    started = []
    monkeypatch.setattr(media, 'enqueue', lambda vid, force=False: started.append(vid))
    media.resume_pending()
    assert started == []


def test_a_replaced_movie_waits_for_the_rebuild(client, review, monkeypatch):
    age()
    client.server.clear_cache(set())
    started = []
    monkeypatch.setattr(media, 'enqueue', lambda vid, force=False: started.append(vid))
    event = {'entity': {'type': 'Version', 'id': VID}, 'attribute_name': 'sg_uploaded_movie', 'meta': {'new_value': {}}}
    body = json.dumps({'data': event}).encode()
    headers = {'X-SG-SIGNATURE': 'sha1=' + hmac.new(b'hook-secret', body, hashlib.sha1).hexdigest(), 'Content-Type': 'application/json'}
    assert client.post('/hooks/shotgrid', content=body, headers=headers).status_code == 200
    assert started == []


def test_export_waits_for_the_rebuild(client, review):
    age()
    client.server.clear_cache(set())
    token = review['token']
    jid = client.post(f'/api/r/{token}/export', json={'kind': 'zip'}).json()['id']
    for _ in range(600):
        s = client.get(f'/api/r/{token}/export/{jid}').json()
        if s['status'] != 'running':
            break
        real_sleep(.05)
    assert s['status'] == 'done', s
    names = zipfile.ZipFile(io.BytesIO(client.get(f'/api/r/{token}/export/{jid}/file').content)).namelist()
    assert len(names) == 1 and names[0].endswith('.png')


def test_publish_waits_for_the_rebuild(client, review, fake):
    age()
    client.server.clear_cache(set())
    results = client.server.publish_review(db.review_by_token(review['token']), {review['item']}, {})
    assert not any(r.get('error') for r in results), results
    assert db.media(VID)['status'] == 'ready'
    assert db.q1('SELECT sg_note_id FROM notes WHERE id=?', (review['note']['id'],))['sg_note_id']


def test_a_movie_gone_from_shotgrid_keeps_the_notes(client, review, monkeypatch):
    age()
    client.server.clear_cache(set())
    monkeypatch.setattr(media.sg_bridge, 'download_movie', lambda vid, dest: (None, None, False))
    opened = client.get(f"/api/r/{review['token']}").json()
    wait_for_ingest()
    assert db.media(VID)['status'] == 'nomedia'
    assert [n['shapes'] for n in opened['notes']] == [SHAPE]


def test_proxies_from_before_the_cleanup_get_the_full_period(client):
    db.ensure_media_row(55)
    db.ex('UPDATE media SET last_used=NULL WHERE version_id=55')
    db.init()
    assert db.media(55)['last_used'] > time.time() - 60
