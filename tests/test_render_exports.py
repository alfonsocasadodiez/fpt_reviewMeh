import csv
import importlib.util
import io
import math
import subprocess
import time
import zipfile

import pytest
from PIL import Image, ImageChops

from conftest import signed_in
from reviewmeh import db, media, render

real_sleep = time.sleep  # the fixtures turn time.sleep off, for publish's pacing


def test_strokes_are_solid():
    """Pillow's wide lines left teeth along short segments; strokes are stamped instead."""
    n = 120
    size = (1920, 1080)
    s = {'t': 'pen', 'c': '#ff3b30', 'w': 0.024, 'pts': [[.1 + .8 * i / n, .5 + .25 * math.sin(i / 14), .5] for i in range(n + 1)]}
    a = render.overlay(size, [s]).getchannel('A')
    r = s['w'] * size[1] / 2
    holes = 0
    for i in range(2, n - 1):
        x0, y0 = s['pts'][i][0] * size[0], s['pts'][i][1] * size[1]
        x1, y1 = s['pts'][i + 1][0] * size[0], s['pts'][i + 1][1] * size[1]
        length = math.hypot(x1 - x0, y1 - y0)
        for f in (-.7, 0, .7):
            holes += a.getpixel((int(x0 - (y1 - y0) / length * r * f), int(y0 + (x1 - x0) / length * r * f))) < 200
    assert holes == 0


def test_opacity_is_flat_and_matches():
    square = {'t': 'rect', 'c': '#ffffff', 'w': 0.1, 'a': [.2, .2], 'b': [.8, .8]}
    solid = render.overlay((400, 400), [square]).getchannel('A').getpixel((200, 100))
    half = render.overlay((400, 400), [{**square, 'o': 0.5}]).getchannel('A').getpixel((200, 100))
    assert solid == 255 and 120 <= half <= 135


def test_bad_opacity_is_harmless():
    assert render.overlay((100, 100), [{'t': 'rect', 'c': '#fff', 'w': .1, 'a': [0, 0], 'b': [1, 1], 'o': 'nonsense'}]).getbbox()


def test_empty_overlay():
    assert render.overlay((100, 100), []).getbbox() is None


def test_seek_is_frame_exact(movie, monkeypatch, tmp_path):
    """The fast seek must give the same picture as decoding from the start."""
    monkeypatch.setattr(media, 'proxy_path', lambda vid: str(movie))
    monkeypatch.setattr(media.db, 'media', lambda vid: {'fps': 24})
    for f in (0, 1, 9, 10, 11, 23, 24, 47):
        old = tmp_path / 'old.png'
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(movie), '-vf', f'select=eq(n\\,{f})', '-fps_mode', 'passthrough', '-frames:v', '1', str(old)], check=True)
        new = media.extract_frame(1, f, str(tmp_path / 'new.png'))
        assert ImageChops.difference(Image.open(old).convert('RGB'), Image.open(new).convert('RGB')).getbbox() is None, f


@pytest.fixture()
def review(client, movie, monkeypatch):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100', 'project_id': '7'})
    db.ex("UPDATE media SET status='ready', fps=24, frame_count=48, width=320, height=180")
    monkeypatch.setattr(media, 'proxy_path', lambda vid: str(movie))
    signed_in(client, 1, login='ada@test', name='Ada')
    r = db.q1('SELECT * FROM reviews')
    item = db.q1('SELECT id FROM items')['id']
    shape = [{'t': 'rect', 'c': '#ffffff', 'w': .02, 'a': [.2, .2], 'b': [.6, .6]}]
    top = client.post(f"/api/r/{r['token']}/notes", json={'item_id': item, 'frame': 5, 'text': '=HYPERLINK("http://x")', 'shapes': shape}).json()
    client.post(f"/api/r/{r['token']}/notes", json={'parent_id': top['id'], 'text': 'agreed <b>'})
    client.post(f"/api/r/{r['token']}/notes", json={'item_id': item, 'frame': 9, 'text': 'no drawing'})
    return r


def export(client, review, kind):
    jid = client.post(f"/api/r/{review['token']}/export", json={'kind': kind}).json()['id']
    for _ in range(200):
        s = client.get(f"/api/r/{review['token']}/export/{jid}").json()
        if s['status'] != 'running':
            break
        real_sleep(.05)
    assert s['status'] == 'done', s
    return client.get(f"/api/r/{review['token']}/export/{jid}/file")


def test_csv(client, review):
    text = export(client, review, 'csv').content.decode('utf-8-sig')
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 3
    assert rows[0]['Text'].startswith("'=HYPERLINK")  # can't run as a formula in a spreadsheet
    assert [r['Type'] for r in rows] == ['Note', 'Reply', 'Note']


def test_zip_has_one_frame_per_drawing(client, review):
    z = zipfile.ZipFile(io.BytesIO(export(client, review, 'zip').content))
    assert len(z.namelist()) == 1 and z.namelist()[0].startswith('sh010_anim_v001/') and z.testzip() is None


@pytest.mark.skipif(importlib.util.find_spec('weasyprint') is None, reason='weasyprint not installed')
def test_pdf(client, review):
    assert export(client, review, 'pdf').content[:4] == b'%PDF'


def test_exports_need_download_and_stay_private(client, review):
    jid = client.post(f"/api/r/{review['token']}/export", json={'kind': 'csv'}).json()['id']
    s = client.post(f"/api/r/{review['token']}/shares", json={'days': 1, 'guests': True}).json()
    client.cookies.clear()
    assert client.post(f"/api/r/{s['key']}/export", json={'kind': 'csv'}).status_code == 403
    assert client.get(f"/api/r/{s['key']}/export/{jid}/file").status_code == 403
    signed_in(client, 1)
    assert client.post(f"/api/r/{review['token']}/export", json={'kind': 'exe'}).status_code == 400
