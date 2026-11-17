"""ShotGrid's own notes and their images on the Shot notes tab, read as the person so ShotGrid's permissions apply."""
import io

import pytest

from conftest import signed_in
from reviewmeh import db, server, sg_bridge

MAX, ART = 'max@test', 'art@test'  # Max may see client notes; Art may not


def attach(fake, att_id, note_id, project_id, name='annot_version_1.8.png', att_project=None, client=False):
    fake.notes.add(note_id)
    fake.note_projects[note_id] = project_id
    fake.note_rows.append({'type': 'Note', 'id': note_id, 'content': f'note {note_id}', 'client_note': client,
                           'note_links': [{'type': 'Version', 'id': 100, 'name': 'sh010_anim_v001'}],
                           'attachments': [{'type': 'Attachment', 'id': att_id, 'name': name}]})
    fake.attachments.append({'type': 'Attachment', 'id': att_id, 'filename': name, 'project': att_project,
                             'attachment_links': [{'type': 'Note', 'id': note_id}], 'this_file': {'url': f'https://sg/file/{att_id}'}})


@pytest.fixture()
def fetched(monkeypatch):
    got = []
    monkeypatch.setattr(server.urllib.request, 'urlopen', lambda url, timeout=0: got.append(url) or io.BytesIO(b'\x89PNG image'))
    return got


def open_review(client, vid='100', project='7'):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': vid, 'project_id': project})
    return db.q1('SELECT token FROM reviews WHERE project_id=?', (int(project),))['token']


def test_a_client_review_site_drawing_with_no_project_shows_through_its_note(fake):
    attach(fake, 900, 60, project_id=7)
    assert sg_bridge.note_attachment_url(900, 7, MAX) == 'https://sg/file/900'


def test_an_image_on_another_projects_note_is_refused(fake):
    attach(fake, 901, 61, project_id=8, att_project={'type': 'Project', 'id': 7})
    assert sg_bridge.note_attachment_url(901, 7, MAX) is None


def test_only_images_on_notes_are_served(fake):
    attach(fake, 902, 62, project_id=7, name='notes.pdf')
    fake.attachments.append({'type': 'Attachment', 'id': 903, 'filename': 'a.png', 'attachment_links': [{'type': 'Version', 'id': 100}], 'this_file': {'url': 'https://sg/x'}})
    assert sg_bridge.note_attachment_url(902, 7, MAX) is None
    assert sg_bridge.note_attachment_url(903, 7, MAX) is None
    assert sg_bridge.note_attachment_url(999, 7, MAX) is None


def test_client_notes_and_their_images_follow_the_persons_shotgrid_rights(client, fake, fetched):
    fake.no_client_notes.add(ART)
    attach(fake, 910, 70, project_id=7, client=True)
    attach(fake, 911, 71, project_id=7)
    token = open_review(client)
    shown = lambda: {(n['id'], n['client']) for n in client.get(f'/api/r/{token}/entity_notes?entity_type=Shot&entity_id=10').json()}

    signed_in(client, 3, login=MAX, name='Max')
    assert shown() == {(70, True), (71, False)}
    assert client.get(f'/api/r/{token}/attachment/910').status_code == 200

    signed_in(client, 2, login=ART, name='Art')
    assert shown() == {(71, False)}
    assert client.get(f'/api/r/{token}/attachment/910').status_code == 404  # though Max's look left a copy on disk
    assert client.get(f'/api/r/{token}/attachment/911').status_code == 200
    assert {c['sudo'] for c in fake.calls} <= {None}  # reading wrote nothing


def test_guests_get_no_shotgrid_notes_even_with_other_versions_allowed(client, fake, fetched):
    attach(fake, 920, 80, project_id=7)
    token = open_review(client)
    signed_in(client, 1, login='ada@test', name='Ada')
    key = client.post(f'/api/r/{token}/shares', json={'guests': True, 'perms': {'versions': True}, 'days': 1}).json()['key']
    client.cookies.clear()
    client.post(f'/api/r/{key}/guest', json={'guest_name': 'Visitor'})
    assert client.get(f'/api/r/{key}/entity_notes?entity_type=Shot&entity_id=10').json() == []
    assert client.get(f'/api/r/{key}/attachment/920').status_code == 403
    assert not fetched


def test_a_kept_copy_is_not_served_to_another_projects_review(client, fake, fetched):
    ours, theirs = open_review(client), open_review(client, '200', '8')
    signed_in(client, 1, login='ada@test', name='Ada')
    attach(fake, 904, 63, project_id=7)
    assert client.get(f'/api/r/{ours}/attachment/904').content == b'\x89PNG image'
    assert client.get(f'/api/r/{theirs}/attachment/904').status_code == 404
    assert client.get(f'/api/r/{ours}/attachment/904').status_code == 200
    assert len(fetched) == 1  # the second look used the kept copy, after asking ShotGrid again
