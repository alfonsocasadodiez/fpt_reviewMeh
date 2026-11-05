"""Reading one field of a review's versions, to sort its list."""
import pytest

from conftest import signed_in
from reviewmeh import db


@pytest.fixture()
def review(client):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100,101', 'project_id': '7'})
    return db.q1('SELECT * FROM reviews')


def guest_link(client, review, **perms):
    signed_in(client, 1)
    key = client.post(f"/api/r/{review['token']}/shares", json={'days': 7, 'guests': True, 'perms': perms}).json()['key']
    client.cookies.clear()
    client.post('/api/identify', json={'token': key, 'guest_name': 'Pat'})
    return key


def test_gives_the_field_of_every_version_in_the_review(client, review):
    signed_in(client, 1)
    r = client.get(f"/api/r/{review['token']}/sort", params={'field': 'code'})
    assert r.status_code == 200
    assert r.json()['vals'] == {'100': 'sh010_anim_v001', '101': 'sh020_anim_v001'}


def test_entity_fields_sort_by_their_name(client, review):
    signed_in(client, 1)
    assert client.get(f"/api/r/{review['token']}/sort", params={'field': 'entity'}).json()['vals'] == {'100': 'sh010', '101': 'sh020'}


def test_reads_a_linked_field(client, review):
    signed_in(client, 1)
    r = client.get(f"/api/r/{review['token']}/sort", params={'field': 'entity.Shot.sg_cut_order'})
    assert r.status_code == 200 and r.json()['def']['key'] == 'entity.Shot.sg_cut_order'


def test_unknown_fields_are_refused(client, review):
    signed_in(client, 1)
    for field in ('nope', 'entity.Shot.nope', 'entity.Nope.code', 'user.HumanUser.password'):
        assert client.get(f"/api/r/{review['token']}/sort", params={'field': field}).status_code == 404


def test_guests_get_only_the_fields_details_gives_them(client, review):
    key = guest_link(client, review, details=True)
    assert client.get(f'/api/r/{key}/sort', params={'field': 'code'}).status_code == 200
    assert client.get(f'/api/r/{key}/sort', params={'field': 'sg_path_to_movie'}).status_code == 403
    assert client.get(f'/api/r/{key}/sort', params={'field': 'entity.Shot.sg_cut_order'}).status_code == 403


def test_a_link_without_details_cant_read_fields(client, review):
    signed_in(client, 1)
    key = client.post(f"/api/r/{review['token']}/shares", json={'days': 7, 'guests': True, 'perms': {'comment': True}}).json()['key']
    client.cookies.clear()
    client.post('/api/identify', json={'token': key, 'guest_name': 'Pat'})
    assert client.get(f'/api/r/{key}/sort', params={'field': 'code'}).status_code == 403


def test_the_page_names_its_sort_fields(client, review):
    signed_in(client, 1)
    assert client.get(f"/api/r/{review['token']}").json()['sort_fields'] == {'task_order': 'sg_task.Task.sg_sort_order', 'cut_order': 'entity.Shot.sg_cut_order'}
