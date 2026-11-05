"""Everything runs against FakeShotgrid. Importing the app with a real site is blocked by the settings below."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_data = tempfile.mkdtemp(prefix='reviewmeh_test_')
os.environ.update({
    'REVIEWMEH_CONFIG': str(Path(_data) / 'none.toml'),
    'REVIEWMEH_SG_URL': 'https://fake.invalid',
    'REVIEWMEH_SG_SCRIPT_NAME': 'test',
    'REVIEWMEH_SG_SCRIPT_KEY': 'test',
    'REVIEWMEH_SECRET': 'test-secret',
    'REVIEWMEH_PUBLIC_URL': 'https://review.test',
    'REVIEWMEH_DATA_DIR': _data,
    'REVIEWMEH_AMI_KEY': 'ami-key',
    'REVIEWMEH_WEBHOOK_SECRET': 'hook-secret',
})

import pytest

from fake_shotgrid import FakeShotgrid


@pytest.fixture()
def fake(monkeypatch):
    from reviewmeh import media, sg_bridge
    sg = FakeShotgrid()
    monkeypatch.setattr(media, 'enqueue', lambda *a, **kw: None)  # no real ingest: tests set the media rows they need
    monkeypatch.setattr(sg_bridge, 'sg', sg.as_user)
    for cache in (sg_bridge._cut_cache, sg_bridge._episode_cache, sg_bridge._status_cache, sg_bridge._fields_cache):
        cache.clear()
    monkeypatch.setattr('time.sleep', lambda s: None)
    return sg


@pytest.fixture()
def client(fake):
    from fastapi.testclient import TestClient
    from reviewmeh import db, server
    with TestClient(server.app, base_url='https://review.test') as c:
        for table in ('note_files', 'notes', 'shares', 'drafts', 'items', 'media', 'reviews', 'settings'):
            db.ex(f'DELETE FROM {table}')
        server._group_rights.clear()
        server._group_rights.update(server.GROUP_RIGHTS)
        server.hub.rooms.clear()
        c.server = server
        yield c


def signed_in(client, user_id, login='person@test', name='Person'):
    """Log this client in as a verified ShotGrid user."""
    cookie = client.server.signer.dumps({'id': user_id, 'name': name, 'login': login, 'v': 1})
    client.cookies.set(client.server.COOKIE, cookie)
    return client


@pytest.fixture()
def movie(tmp_path_factory):
    """A real, small proxy: 24 fps, a different picture each frame."""
    if not shutil.which('ffmpeg'):
        pytest.skip('ffmpeg not installed')
    path = tmp_path_factory.mktemp('movie') / 'proxy.mp4'
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=24:duration=2', '-c:v', 'libx264', '-g', '10', '-bf', '0',
                    '-pix_fmt', 'yuv420p', str(path)], check=True)
    return path
