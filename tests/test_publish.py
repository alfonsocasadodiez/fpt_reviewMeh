"""The thread ShotGrid ends up with: Note, then Replies, with each frame and file right after its text."""
import os
import re
import time

import pytest

from conftest import signed_in
from reviewmeh import db, media


@pytest.fixture()
def review(client, movie, monkeypatch):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100,101', 'project_id': '7'})
    db.ex("UPDATE media SET status='ready', fps=24, frame_count=48, width=320, height=180")
    monkeypatch.setattr(media, 'proxy_path', lambda vid: str(movie))
    signed_in(client, 1, login='ada@test', name='Ada')
    return db.q1('SELECT * FROM reviews')


SHAPE = [{'t': 'rect', 'c': '#ffffff', 'w': 0.02, 'a': [.2, .2], 'b': [.6, .6]}]


def note(client, review, item, frame, text='', shapes=None, **kw):
    r = client.post(f"/api/r/{review['token']}/notes", json={'item_id': item, 'frame': frame, 'text': text, 'shapes': shapes or [], **kw})
    assert r.status_code == 200, r.text
    return r.json()


def items():
    return [r['id'] for r in db.q('SELECT id FROM items ORDER BY id')]


def publish(client, review, item_ids):
    from reviewmeh import server
    return server.publish_review(db.review_by_token(review['token']), set(item_ids), {})


def shape_of(fake):
    return ''.join({'Note': 'N', 'Reply': 'R'}.get(c.get('entity'), '?') if c['kind'] == 'create' else ('F' if c['file'].startswith('annot_') else 'A') for c in fake.writes())


def test_first_note_then_replies_in_frame_order(client, review, fake):
    it = items()[0]
    note(client, review, it, 20, 'later', SHAPE)
    note(client, review, it, 5, 'first', SHAPE)
    note(client, review, it, 10, 'text only')
    res = publish(client, review, [it])[0]
    assert (res['notes'], res['replies'], res['attachments']) == (1, 2, 2)
    assert shape_of(fake) == 'NFRRF'
    n = fake.writes('create')[0]
    assert '[f6 ' in n['data']['content'] and 'first' in n['data']['content']
    replies = fake.writes('create')[1:]
    assert all(r['data']['entity']['id'] == n['id'] for r in replies)
    assert 'text only' in replies[0]['data']['content'] and 'later' in replies[1]['data']['content']
    assert all(u['entity'] == 'Note' and u['entity_id'] == n['id'] for u in fake.writes('upload'))


def test_first_note_links_back_to_the_version(client, review, fake):
    it = items()[0]
    note(client, review, it, 1, 'hello')
    publish(client, review, [it])
    assert f"https://review.test/r/{review['token']}?item={it}" in fake.writes('create')[0]['data']['content']


def test_author_and_guest(client, review, fake):
    it = items()[0]
    note(client, review, it, 1, 'by ada')
    s = client.post(f"/api/r/{review['token']}/shares", json={'days': 1, 'guests': True, 'perms': {'comment': True}}).json()
    client.cookies.clear()
    client.post('/api/identify', json={'token': s['key'], 'guest_name': 'Pat'})
    assert client.post(f"/api/r/{s['key']}/notes", json={'item_id': it, 'frame': 3, 'text': 'by pat'}).status_code == 200
    publish(client, review, [it])
    first, reply = fake.writes('create')
    assert first['sudo'] == 'ada@test'
    assert reply['sudo'] is None and reply['data']['content'].startswith('Pat (guest): ')


def test_sudo_refused_falls_back_to_the_script(client, review, fake):
    fake.refuse_sudo.add('ada@test')
    it = items()[0]
    note(client, review, it, 1, 'one')
    note(client, review, it, 2, 'two')
    publish(client, review, [it])
    first, reply = fake.writes('create')
    assert first['sudo'] is None and 'Review notes by Ada' in first['data']['content']
    assert reply['sudo'] is None and reply['data']['content'].startswith('Ada: ')


def test_second_publish_continues_the_thread_and_repeats_nothing(client, review, fake):
    it = items()[0]
    note(client, review, it, 1, 'one')
    publish(client, review, [it])
    thread = fake.writes('create')[0]['id']
    assert publish(client, review, [it]) == []
    assert len(fake.writes()) == 1
    note(client, review, it, 9, 'two', SHAPE)
    publish(client, review, [it])
    assert [c.get('entity') for c in fake.writes('create')] == ['Note', 'Reply']
    assert fake.writes('create')[1]['data']['entity']['id'] == thread


def test_deleted_thread_starts_a_new_one(client, review, fake):
    it = items()[0]
    note(client, review, it, 1, 'one')
    publish(client, review, [it])
    fake.notes.clear()  # someone deleted the Note in ShotGrid
    note(client, review, it, 2, 'two')
    publish(client, review, [it])
    assert [c['entity'] for c in fake.writes('create')] == ['Note', 'Note']


def test_reply_to_a_deleted_note_still_goes_out(client, review, fake):
    it = items()[0]
    top = note(client, review, it, 1, 'one')
    publish(client, review, [it])
    fake.notes.clear()
    note(client, review, it, 1, 'late reply', parent_id=top['id'])
    res = publish(client, review, [it])[0]
    assert 'error' not in res and fake.writes('create')[-1]['entity'] == 'Note'
    assert not [n for n in db.item_notes(it) if not n['sg_note_id'] and not n['sg_reply_id']]


def test_attachments_keep_their_names(client, review, fake):
    it = items()[0]
    f = client.post(f"/api/r/{review['token']}/files?name=ref sheet.pdf", content=b'%PDF').json()
    note(client, review, it, 1, 'see file', file_ids=[f['id']])
    publish(client, review, [it])
    up = fake.writes('upload')[0]
    assert up['file'] == 'ref sheet.pdf' and up['display_name'] == 'ref sheet.pdf'
    assert db.file_row(f['id'])['sg_attachment_id']


def test_failed_upload_resumes_without_repeating(client, review, fake):
    it = items()[0]
    f = client.post(f"/api/r/{review['token']}/files?name=flaky.pdf", content=b'%PDF').json()
    n = note(client, review, it, 1, 'x', file_ids=[f['id']])
    fake.fail_uploads.append('flaky.pdf')
    assert 'error' in publish(client, review, [it])[0]
    assert db.note(n['id'])['sg_unsent'] is True
    assert 'error' not in publish(client, review, [it])[0]
    assert len(fake.writes('create')) == 1 and [u['file'] for u in fake.writes('upload')] == ['flaky.pdf']
    assert db.note(n['id'])['sg_unsent'] is False


def test_failed_frame_resumes(client, review, fake):
    it = items()[0]
    n = note(client, review, it, 1, 'x', SHAPE)
    original = fake.as_user('ada@test').upload
    calls = []

    def fail_once(*a, **kw):
        if not calls:
            calls.append(1)
            raise RuntimeError('upload failed')
        return original(*a, **kw)
    fake.as_user('ada@test').upload = fail_once
    assert 'error' in publish(client, review, [it])[0]
    assert db.note(n['id'])['sg_unsent'] is True
    publish(client, review, [it])
    assert len(fake.writes('create')) == 1 and len(fake.writes('upload')) == 1


def test_versions_get_their_own_threads(client, review, fake):
    a, b = items()
    for it in (a, b):
        note(client, review, it, 1, f'top {it}', SHAPE)
        note(client, review, it, 2, f'next {it}', SHAPE)
    res = publish(client, review, [a, b])
    assert [r['item_id'] for r in res] == [a, b] and all('error' not in r for r in res)
    notes = [c for c in fake.writes('create') if c['entity'] == 'Note']
    assert len(notes) == 2
    for n in notes:
        replies = [c for c in fake.writes('create') if c['entity'] == 'Reply' and c['data']['entity']['id'] == n['id']]
        assert len(replies) == 1 and re.search(r'top (\d+)', n['data']['content']).group(1) in replies[0]['data']['content']


def test_publish_needs_the_right(client, review):
    signed_in(client, 2)
    assert client.post(f"/api/r/{review['token']}/publish", json={'item_ids': items()}).status_code == 403


def test_publish_job_reports_results(client, review, fake):
    it = items()[0]
    note(client, review, it, 1, 'one')
    jid = client.post(f"/api/r/{review['token']}/publish", json={'item_ids': [it]}).json()['id']
    for _ in range(50):
        s = client.get(f"/api/r/{review['token']}/publish/{jid}").json()
        if s['status'] != 'running':
            break
    assert s['status'] == 'done' and s['results'][0]['notes'] == 1


def test_no_temp_frames_left_behind(client, review, fake, tmp_path, monkeypatch):
    import tempfile
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    it = items()[0]
    note(client, review, it, 1, 'x', SHAPE)
    publish(client, review, [it])
    assert not os.listdir(tmp_path)
