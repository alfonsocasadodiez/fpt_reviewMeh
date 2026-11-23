import pytest

from conftest import signed_in
from reviewmeh import db


@pytest.fixture()
def review(client):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100', 'project_id': '7'})
    return db.q1('SELECT * FROM reviews')


def join(client, review, user_id):
    signed_in(client, user_id)
    ws = client.websocket_connect(f"/ws/{review['token']}").__enter__()
    cid = next_of(ws, 'welcome')['cid']
    return ws, cid


def leave(*sockets):
    for ws in sockets:
        ws.__exit__(None, None, None)


def next_of(ws, kind):
    while True:
        m = ws.receive_json()
        if m['type'] == kind:
            return m


def presented(ws):
    """Reads up to the presenter's state; returns it with the presenter as the last presence before it showed them."""
    who = None
    while (m := ws.receive_json())['type'] != 'present':
        if m['type'] == 'presence':
            who = next((u for u in m['users'] if u['presenting']), who)
    return m, who


def relayed(sender, receiver):
    """What reached the receiver from the sender's last messages: the sender's 'view' answers with presence, which marks the end."""
    sender.send_json({'type': 'view', 'item_id': None})
    got = []
    while (m := receiver.receive_json())['type'] != 'presence':
        got.append(m['type'])
    return got


STATE = {'vids': [100], 'i': 0, 'frame': 12, 'playing': False, 'rate': 1}


def test_a_laser_trace_reaches_the_others_and_is_never_saved(client, review):
    a, _ = join(client, review, 1)
    b, _ = join(client, review, 2)
    a.send_json({'type': 'laser', 'v': 100, 'c': '#ff9500', 'start': True, 'pts': [[0.123456, 0.5], [0.2, 0.25]]})
    m = next_of(b, 'laser')
    assert m['pts'] == [[0.1235, 0.5], [0.2, 0.25]] and m['start'] is True and m['v'] == 100 and m['c'] == '#ff9500' and m['name']
    assert db.q('SELECT * FROM notes') == []
    leave(a, b)


def test_everyone_following_can_steer_the_presentation_by_default(client, review):
    p, pcid = join(client, review, 1)
    f, _ = join(client, review, 2)
    p.send_json({'type': 'present', 'state': STATE})
    m, presenter = presented(f)
    assert m['state']['frame'] == 12 and presenter['control'] == 'all'
    f.send_json({'type': 'control', 'state': {**STATE, 'frame': 40, 'playing': True}})
    m = next_of(p, 'control')
    assert m['from'] == pcid and m['state']['frame'] == 40 and m['by']
    leave(p, f)


def test_the_presenter_can_keep_control_or_hand_it_to_some(client, review):
    p, _ = join(client, review, 1)
    f, _ = join(client, review, 2)
    o, _ = join(client, review, 3)
    p.send_json({'type': 'present', 'state': STATE, 'control': []})
    next_of(o, 'present')
    f.send_json({'type': 'control', 'state': {**STATE, 'frame': 40}})
    assert relayed(f, o) == []  # dropped: only the presenter controls

    p.send_json({'type': 'present', 'state': STATE, 'control': ['u2']})
    assert presented(o)[1]['control'] == ['u2']
    f.send_json({'type': 'control', 'state': {**STATE, 'frame': 40}})
    assert next_of(o, 'control')['state']['frame'] == 40
    o.send_json({'type': 'control', 'state': {**STATE, 'frame': 80}})  # user 3 isn't picked
    assert relayed(o, f) == []
    leave(p, f, o)
