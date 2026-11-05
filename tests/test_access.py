import pytest

from conftest import signed_in
from reviewmeh import db


@pytest.fixture()
def review(client):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100', 'project_id': '7'})
    return db.q1('SELECT * FROM reviews')


def share(client, review, **body):
    signed_in(client, 1)
    r = client.post(f"/api/r/{review['token']}/shares", json={'days': 7, **body})
    client.cookies.clear()
    return r


def test_studio_link_needs_sign_in(client, review):
    assert client.get(f"/api/r/{review['token']}").status_code == 401
    assert client.get(f"/api/gate/{review['token']}").json() == {'guests': False, 'password': False}


def test_rights_come_from_the_group(client, review):
    signed_in(client, 2)
    perms = client.get(f"/api/r/{review['token']}").json()['access']['perms']
    assert 'comment' in perms and 'publish' not in perms and 'status' not in perms
    assert client.post(f"/api/r/{review['token']}/publish", json={'item_ids': []}).status_code == 403


def test_unknown_group_and_inactive_account_get_nothing(client, review):
    signed_in(client, 5)
    assert client.get(f"/api/r/{review['token']}").status_code == 403
    signed_in(client, 4)  # deactivated: treated as not signed in
    assert client.get('/api/me').json()['user'] is None


def test_admin_group_cannot_be_locked_out(client):
    signed_in(client, 1)
    assert client.put('/api/access', json={'group': 'Admin', 'rights': []}).status_code == 400
    assert client.put('/api/access', json={'group': 'Artist', 'rights': ['view']}).status_code == 200
    signed_in(client, 2)
    assert client.get('/api/access').status_code == 403


def test_links_have_to_expire_within_the_limit(client, review):
    assert share(client, review, days=0).status_code == 400
    assert share(client, review, days=31).status_code == 400
    assert share(client, review, days=30).status_code == 200


def test_guests_are_off_by_default(client, review):
    key = share(client, review).json()['key']
    assert client.get(f'/api/r/{key}').status_code == 401
    assert client.post('/api/identify', json={'token': key, 'guest_name': 'x'}).status_code == 403


def test_guest_rights_follow_the_link(client, review):
    key = share(client, review, guests=True, perms={'comment': True}).json()['key']
    assert client.post('/api/identify', json={'token': key, 'guest_name': 'Pat'}).json()['user']['name'] == 'Pat (guest)'
    assert client.get(f'/api/r/{key}').json()['access']['perms'] == ['comment', 'view']
    assert client.get(f'/api/r/{key}/media/100/proxy.mp4?dl=1').status_code == 403
    assert client.get(f'/api/r/{key}/fields').status_code == 403
    assert client.post(f'/api/r/{key}/files?name=a.txt', content=b'x').status_code == 403


def test_guest_cannot_claim_a_real_name(client, review):
    key = share(client, review, guests=True).json()['key']
    me = client.post('/api/identify', json={'token': key, 'guest_name': 'Ada (guest)'}).json()['user']
    assert me == {'id': None, 'name': 'Ada (guest)', 'login': None, 'v': 0}


def test_guest_password(client, review):
    r = share(client, review, guests=True, password='open sesame')
    key = r.json()['key']
    assert 'open sesame' not in r.text and 'password_hash' not in r.text
    assert client.get(f'/api/r/{key}').status_code == 401
    assert client.get(f'/api/gate/{key}').json()['password'] is True
    assert client.post('/api/identify', json={'token': key, 'guest_name': 'x', 'password': 'nope'}).status_code == 403
    assert client.post('/api/identify', json={'token': key, 'guest_name': 'x', 'password': 'open sesame'}).status_code == 200
    assert client.get(f'/api/r/{key}').status_code == 200


def test_password_guessing_is_limited(client, review):
    key = share(client, review, guests=True, password='open sesame').json()['key']
    codes = [client.post('/api/identify', json={'token': key, 'guest_name': 'x', 'password': f'bad{i}'}).status_code for i in range(7)]
    assert codes[:5] == [403] * 5 and codes[5:] == [429] * 2


def test_expired_and_revoked_links(client, review):
    s = share(client, review, guests=True).json()
    db.ex('UPDATE shares SET expires_at=1 WHERE id=?', (s['id'],))
    assert client.get(f"/api/r/{s['key']}").status_code == 410
    s = share(client, review, guests=True).json()
    signed_in(client, 1)
    client.delete(f"/api/r/{review['token']}/shares/{s['id']}")
    client.cookies.clear()
    assert client.get(f"/api/r/{s['key']}").status_code == 404


def test_share_link_never_reveals_the_studio_token(client, review):
    key = share(client, review, guests=True).json()['key']
    r = client.get(f'/api/r/{key}')
    assert review['token'] not in r.text and r.json()['studio_url'] is None


def test_share_link_stays_inside_the_review(client, review):
    key = share(client, review, guests=True).json()['key']
    assert client.get(f'/api/r/{key}/media/101/thumb.jpg').status_code == 404


def test_uploads(client, review):
    signed_in(client, 2)
    url = f"/api/r/{review['token']}/files"
    assert client.post(f'{url}?name=evil.html', content=b'x').status_code == 415
    assert client.post(f'{url}?name=evil.SVG', content=b'x').status_code == 415
    f = client.post(f'{url}?name=../../pic.png', content=b'data').json()
    assert f['name'] == 'pic.png'
    got = client.get(f"{url}/{f['id']}")
    assert got.headers['content-disposition'].startswith('inline') and got.headers['x-content-type-options'] == 'nosniff'
    other = client.post(f'{url}?name=notes.pdf', content=b'data').json()
    assert client.get(f"{url}/{other['id']}").headers['content-type'] == 'application/octet-stream'
    signed_in(client, 3)  # someone else's unposted upload
    assert client.get(f"{url}/{f['id']}").status_code == 404
    assert client.delete(f"{url}/{f['id']}").status_code == 404
    item = db.q1('SELECT id FROM items')['id']
    n = client.post(f"/api/r/{review['token']}/notes", json={'item_id': item, 'text': 'x', 'file_ids': [f['id']]}).json()
    assert n['files'] == []  # can't take over another person's upload


def test_config_js_has_nothing_secret(client):
    js = client.get('/config.js').text
    for secret in ('test-secret', 'ami-key', 'hook-secret'):
        assert secret not in js


ADMIN_ONLY = [('get', '/api/access', None), ('put', '/api/access', {'group': 'Artist', 'rights': ['view']}),
              ('get', '/api/transcode', None), ('put', '/api/transcode', {'crf': 12, 'max_width': 1920}),
              ('get', '/api/transcode/status', None), ('post', '/api/transcode/clear', None),
              ('post', '/api/r/{token}/clear-movies', None)]


def call(client, method, path, body, token):
    url = path.format(token=token)
    return client.request(method.upper(), url, **({'json': body} if body else {}))


@pytest.mark.parametrize('method,path,body', ADMIN_ONLY)
def test_admin_pages_refuse_everyone_else(client, review, method, path, body):
    client.cookies.clear()
    assert call(client, method, path, body, review['token']).status_code == 403  # signed out
    for user in (2, 3):  # Artist, Manager
        signed_in(client, user)
        assert call(client, method, path, body, review['token']).status_code == 403
    client.cookies.clear()
    key = share(client, review, guests=True, perms={'details': True, 'comment': True}).json()['key']
    client.post('/api/identify', json={'token': key, 'guest_name': 'Pat'})
    assert call(client, method, path, body, key).status_code == 403  # a guest, through a link
    assert call(client, method, path, body, review['token']).status_code == 403


@pytest.mark.parametrize('method,path,body', ADMIN_ONLY)
def test_admin_pages_open_for_admins(client, review, method, path, body):
    signed_in(client, 1)
    assert call(client, method, path, body, review['token']).status_code == 200


def test_the_page_only_tells_admins_they_are(client, fake):
    signed_in(client, 1)
    assert client.get('/api/me').json()['admin'] is True
    signed_in(client, 3)
    assert client.get('/api/me').json()['admin'] is False
