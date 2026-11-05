import hashlib
import hmac
import json

from conftest import signed_in
from reviewmeh import db, hooks

FORM = {'entity_type': 'Version', 'selected_ids': '100,101', 'user_id': '1', 'project_id': '7', 'project_name': 'Show'}


def ami(client, action='review', key='ami-key', data=FORM):
    return client.post(f'/ami/{action}?key={key}', data=data)


def test_ami_needs_the_key(client):
    assert ami(client, key='wrong').status_code == 403
    assert client.post('/ami/review', data=FORM).status_code == 403
    assert not db.q('SELECT 1 FROM reviews')


def test_ami_never_signs_anyone_in(client):
    r = ami(client)
    assert r.status_code == 200
    assert 'auth=' not in r.text
    assert client.server.COOKIE not in r.cookies
    token = db.q1('SELECT token FROM reviews')['token']
    assert f'/r/{token}' in r.text
    assert client.get('/api/me').json()['user'] is None
    assert client.get(f'/api/r/{token}').status_code == 401  # still has to sign in


def test_ami_cannot_add_versions_from_another_project(client):
    ami(client, data={**FORM, 'selected_ids': '100,200'})
    assert [i['version_id'] for i in db.q('SELECT version_id FROM items')] == [100]


def test_ami_playlist_reuses_its_review(client):
    data = {**FORM, 'entity_type': 'Playlist', 'selected_ids': '50'}
    ami(client, data=data)
    ami(client, data=data)
    assert len(db.q('SELECT 1 FROM reviews')) == 1
    assert len(db.q('SELECT 1 FROM items')) == 2


def test_ami_refuses_a_group_without_access(client):
    assert ami(client, data={**FORM, 'user_id': '5'}).status_code == 403


def test_ami_publish_and_export_open_the_dialog_without_writing(client, fake):
    ami(client)
    token = db.q1('SELECT token FROM reviews')['token']
    assert f'/r/{token}?publish=1' in ami(client, 'publish').text
    assert f'/r/{token}?export=1' in ami(client, 'export').text
    assert not fake.writes()


def test_ami_with_nothing_selected(client):
    assert ami(client, data={'entity_type': 'Shot', 'ids': '1'}).status_code == 400
    assert ami(client, 'nonsense').status_code == 404


def signed(secret, payload):
    body = json.dumps(payload).encode()
    return body, {'X-SG-SIGNATURE': 'sha1=' + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest(), 'Content-Type': 'application/json'}


def status_event(version=100, status='apr'):
    return {'data': {'meta': {'type': 'attribute_change', 'entity_type': 'Version', 'entity_id': version, 'attribute_name': 'sg_status_list', 'new_value': status}}}


def test_webhook_refuses_bad_signatures(client):
    body, headers = signed('wrong', status_event())
    assert client.post('/hooks/shotgrid', content=body, headers=headers).status_code == 401
    assert client.post('/hooks/shotgrid', content=body).status_code == 401


def test_webhook_updates_status(client):
    ami(client)
    body, headers = signed('hook-secret', status_event())
    r = client.post('/hooks/shotgrid', content=body, headers=headers)
    assert r.json() == {'ok': True, 'handled': 1}
    assert db.q1('SELECT sg_status FROM items WHERE version_id=100')['sg_status'] == 'apr'


def test_webhook_batches():
    batch = {'data': {'deliveries': [{'data': {'meta': {'entity_id': 1}}}, {'data': {'meta': {'entity_id': 2}}}]}}
    assert [e['entity_id'] for e in hooks.events(batch)] == [1, 2]
    assert hooks.events({'data': 'nonsense'}) == [] and hooks.events([]) == []


def test_webhook_ignores_other_events(client):
    body, headers = signed('hook-secret', {'data': {'meta': {'entity_type': 'Asset', 'entity_id': 1, 'attribute_name': 'code'}}})
    assert client.post('/hooks/shotgrid', content=body, headers=headers).json()['handled'] == 0
