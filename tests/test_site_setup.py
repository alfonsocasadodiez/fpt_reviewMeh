"""Sites differ: permission groups, statuses, what groups a cut, and how much of the setup a container has to be told."""
import json
import os
import stat
import subprocess
import sys

import pytest

from conftest import signed_in
from reviewmeh import cli, config, db, server, sg_bridge

EP = {'type': 'Episode', 'id': 1, 'name': 'ep01'}
SEQ = {'type': 'Sequence', 'id': 2, 'name': 'sq01'}
SCENE = {'type': 'Scene', 'id': 3, 'name': 'sc01'}
FULL_SCHEMA = {'Shot': {'sg_sequence': 'Sequence', 'sg_scene': 'Scene'}, 'Sequence': {'episode': 'Episode'}, 'Scene': {'sg_sequence': 'Sequence'}}


def shot(id_, project=7, order=1, status='ip', **links):
    return {'type': 'Shot', 'id': id_, 'code': f'sh{id_}', 'project': {'type': 'Project', 'id': project}, 'sg_status_list': status, 'sg_cut_order': order, **links}


@pytest.fixture()
def site(fake):
    fake.links = dict(FULL_SCHEMA)
    fake.parents = {'Episode': [{'type': 'Episode', 'id': 1, 'code': 'ep01'}], 'Sequence': [{'type': 'Sequence', 'id': 2, 'code': 'sq01'}],
                    'Scene': [{'type': 'Scene', 'id': 3, 'code': 'sc01'}]}
    return fake


@pytest.fixture()
def review(client):
    client.post('/ami/review?key=ami-key', data={'entity_type': 'Version', 'selected_ids': '100', 'project_id': '7'})
    return db.q1('SELECT * FROM reviews')


# ─── cuts ─────────────────────────────────────────────────────────────

def test_each_project_on_a_site_uses_what_its_shots_are_linked_to(site):
    site.shots = [shot(10, 7, **{'sg_sequence.Sequence.episode': EP, 'sg_sequence': SEQ}), shot(20, 8, sg_sequence=SEQ), shot(40, 6)]
    assert sg_bridge.project_cut(7)['label'] == 'Episode'
    assert sg_bridge.project_cut(8)['label'] == 'Sequence'
    assert sg_bridge.project_cut(6) is None


def test_a_link_to_a_scene_is_not_taken_for_part_of_the_hierarchy(site):
    site.links['Scene'] = {'sg_sequence': 'Sequence', 'sg_episode': 'Episode'}
    site.shots = [shot(10, sg_scene=SCENE, **{'sg_scene.Scene.sg_sequence': SEQ, 'sg_scene.Scene.sg_episode': EP})]
    assert not [lv for lv in sg_bridge.cut_levels() if 'Scene' in lv['field'] or lv['type'] == 'Scene']
    assert sg_bridge.project_cut(7) is None


def test_a_studio_with_episodes_made_of_scenes_can_ask_for_them(site, monkeypatch):
    monkeypatch.setattr(config.SCHEMA, 'cut_by', ['episode', 'scene'])
    site.links['Scene'] = {'sg_episode': 'Episode'}
    site.shots = [shot(10, sg_scene=SCENE, **{'sg_scene.Scene.sg_episode': EP})]
    assert sg_bridge.project_cut(7) == {'type': 'Episode', 'field': 'sg_scene.Scene.sg_episode', 'label': 'Episode'}


def test_a_studio_using_scenes_sees_them_under_its_own_name(site, monkeypatch):
    monkeypatch.setattr(config.SCHEMA, 'cut_by', ['scene'])
    site.links = {'Shot': {'sg_scene': 'Scene'}}
    site.entity_names = {'Scene': 'Beat'}
    site.shots = [shot(10, sg_scene=SCENE)]
    assert sg_bridge.project_cut(7) == {'type': 'Scene', 'field': 'sg_scene', 'label': 'Beat'}


def test_cut_by_forces_a_level_and_off_turns_cuts_off(site, monkeypatch):
    site.shots = [shot(10, sg_scene=SCENE, sg_sequence=SEQ, **{'sg_sequence.Sequence.episode': EP})]
    monkeypatch.setattr(config.SCHEMA, 'cut_by', ['scene'])
    assert sg_bridge.project_cut(7)['type'] == 'Scene'
    sg_bridge._episode_cache.clear()
    monkeypatch.setattr(config.SCHEMA, 'cut_by', ['off'])
    assert sg_bridge.cut_levels() == [] and sg_bridge.project_cut(7) is None


def test_a_custom_entity_can_be_named_outright(site, monkeypatch):
    for key, value in (('cut_parent_type', 'CustomEntity01'), ('cut_parent_field', 'sg_reel'), ('cut_label', 'Reel')):
        monkeypatch.setattr(config.SCHEMA, key, value)
    assert sg_bridge.project_cut(7) == {'type': 'CustomEntity01', 'field': 'sg_reel', 'label': 'Reel'}


def test_review_gets_the_label_and_the_cut_in_order_without_omitted_shots(client, site, review, monkeypatch):
    site.links = {'Shot': {'sg_scene': 'Scene'}}
    site.entity_names = {'Scene': 'Beat'}
    site.shots = [shot(20, order=2, sg_scene=SCENE), shot(10, order=1, sg_scene=SCENE), shot(30, order=3, sg_scene=SCENE, status='cut')]
    monkeypatch.setattr(config.SCHEMA, 'cut_by', ['scene'])
    monkeypatch.setattr(config.SCHEMA, 'omit_statuses', ['omt', 'cut'])
    signed_in(client, 1)
    assert client.get(f"/api/r/{review['token']}").json()['cut_label'] == 'Beat'
    assert [e['code'] for e in client.get(f"/api/r/{review['token']}/filters").json()['episodes']] == ['sc01']
    cut = client.get(f"/api/r/{review['token']}/cut?shot_id=10").json()
    assert cut['episode'] == {'id': 3, 'code': 'sc01'}
    assert [s['id'] for s in cut['shots']] == [10, 20]  # version 200's shot 30 was never in it, and 30 is omitted


def test_no_cut_for_a_project_with_nothing_linked(client, site, review):
    site.shots = [shot(10)]
    signed_in(client, 1)
    assert client.get(f"/api/r/{review['token']}").json()['cut_label'] is None
    assert client.get(f"/api/r/{review['token']}/cut?shot_id=10").status_code == 404


# ─── statuses ─────────────────────────────────────────────────────────

def test_custom_statuses_work_and_a_new_one_needs_no_restart(client, fake, review, monkeypatch):
    fake.project_statuses[7] = ['rev', 'wip']
    signed_in(client, 1)
    url = f"/api/r/{review['token']}"
    assert [s['code'] for s in client.get(f'{url}/statuses').json()] == ['rev', 'wip']
    assert client.post(f'{url}/versions/100/status', json={'status': 'wip'}).status_code == 200
    assert fake.writes('update')[-1]['data'] == {'sg_status_list': 'wip'}
    assert client.post(f'{url}/versions/100/status', json={'status': 'apr'}).status_code == 400  # not in this project

    fake.project_statuses[7] = ['rev', 'wip', 'new']
    real = sg_bridge.time.time
    monkeypatch.setattr(sg_bridge.time, 'time', lambda: real() + 301)
    assert 'new' in [s['code'] for s in client.get(f'{url}/statuses').json()]


def test_notes_only_get_a_status_the_site_has(fake, monkeypatch):
    assert sg_bridge.note_status() == 'opn'
    sg_bridge._status_cache.clear()
    fake.statuses['Note'] = ['todo', 'done']
    assert sg_bridge.note_status() is None
    sg_bridge._status_cache.clear()
    monkeypatch.setattr(config.SCHEMA, 'note_status', 'todo')
    assert sg_bridge.note_status() == 'todo'


# ─── permission groups ────────────────────────────────────────────────

def test_a_custom_group_can_be_the_admin_group(client, monkeypatch):
    monkeypatch.setattr(server, 'ADMIN_GROUPS', {'Client'})
    signed_in(client, 5)
    assert client.get('/api/me').json()['admin'] is True
    assert client.put('/api/access', json={'group': 'Client', 'rights': []}).status_code == 400
    signed_in(client, 1)
    assert client.get('/api/access').status_code == 403  # "Admin" is just a name now


def test_a_custom_group_is_listed_with_no_access_until_given_some(client, review):
    signed_in(client, 1)
    groups = {g['name']: g for g in client.get('/api/access').json()['groups']}
    assert groups['Client']['rights'] == [] and groups['Client']['users'] == 1
    signed_in(client, 5)
    assert client.get(f"/api/r/{review['token']}").status_code == 403

    signed_in(client, 1)
    assert client.put('/api/access', json={'group': 'Client', 'rights': ['view', 'open']}).status_code == 200
    signed_in(client, 5)
    assert client.get(f"/api/r/{review['token']}").status_code == 200


# ─── acting as people ─────────────────────────────────────────────────

def test_sudo_is_tested_with_a_read(fake):
    assert sg_bridge.sudo_works() is True
    fake.refuse_sudo.update({'ada@test', 'art@test', 'max@test', 'gone@test', 'cleo@test'})
    assert sg_bridge.sudo_works() is False


# ─── reviewmeh check and webhook ──────────────────────────────────────

def run_check(capsys):
    with pytest.raises(SystemExit):
        cli.cmd_check(None)
    return capsys.readouterr().out


def test_check_reports_this_sites_setup(site, capsys, monkeypatch):
    db.ex('DELETE FROM settings')  # rights saved from the Access tab by earlier tests
    monkeypatch.setattr(config, 'ADMIN_GROUPS', {'Studio Admin'})
    site.statuses['Note'] = ['todo']
    site.refuse_sudo.update({'ada@test', 'art@test', 'max@test', 'gone@test', 'cleo@test'})
    out = run_check(capsys)
    assert 'FAIL  nobody is in the admin group Studio Admin' in out
    assert 'Client (1): no access yet' in out
    assert 'Perform actions as though logged in as another user' in out
    assert 'Note status "opn" does not exist' in out
    assert 'Episode: Shot.sg_sequence.Sequence.episode' in out and 'Sequence: Shot.sg_sequence' in out and 'Scene' not in out
    assert 'Version statuses: Review, Approved, Hold' in out


def test_webhook_prints_what_to_paste(capsys):
    cli.cmd_webhook(None)
    out = capsys.readouterr().out
    assert 'https://review.test/hooks/shotgrid' in out and 'hook-secret' in out


# ─── a container with nothing but environment variables ───────────────

def read_config(data_dir, **env):
    code = ('import json; from reviewmeh import config as c; S = c.SCHEMA; print(json.dumps({"secret": c.SECRET, "ami": c.AMI_KEY, "hook": c.WEBHOOK_SECRET, "cut_by": S.cut_by, '
            '"omit": S.omit_statuses, "admins": sorted(c.ADMIN_GROUPS), "groups": c.GROUP_RIGHTS, "ips": c.FORWARDED_ALLOW_IPS, "guest": sorted(c.GUEST_FIELDS)[:1]}))')
    clean = {k: v for k, v in os.environ.items() if not k.startswith('REVIEWMEH_')}
    out = subprocess.run([sys.executable, '-c', code], env={**clean, 'PYTHONPATH': os.pathsep.join(sys.path), 'REVIEWMEH_CONFIG': str(data_dir / 'none.toml'),
                                                            'REVIEWMEH_DATA_DIR': str(data_dir), **env}, capture_output=True, text=True, check=True, cwd=data_dir)
    return json.loads(out.stdout)


def test_secrets_are_made_once_kept_private_and_differ(tmp_path):
    first = read_config(tmp_path)
    assert len({first['secret'], first['ami'], first['hook']}) == 3 and all(len(first[k]) > 30 for k in ('secret', 'ami', 'hook'))
    assert read_config(tmp_path) == first  # same after a restart
    assert stat.S_IMODE((tmp_path / 'secrets').stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / 'secrets' / 'server_secret').stat().st_mode) == 0o600
    assert not [p for p in (tmp_path / 'secrets').iterdir() if p.name.startswith('.')]


def test_a_secret_set_by_hand_wins(tmp_path):
    assert read_config(tmp_path, REVIEWMEH_SECRET='mine')['secret'] == 'mine'
    assert not (tmp_path / 'secrets' / 'server_secret').exists()


def test_every_setting_has_a_variable(tmp_path):
    got = read_config(tmp_path, REVIEWMEH_CUT_BY='Scene, sequence', REVIEWMEH_SCHEMA_OMIT_STATUS='omt,cut', REVIEWMEH_ADMIN_GROUP='Studio Admin, TD',
                      REVIEWMEH_ACCESS_GROUPS='{"Lead": ["view", "open"]}', REVIEWMEH_SERVER_FORWARDED_ALLOW_IPS='172.18.0.2', REVIEWMEH_ACCESS_GUEST_FIELDS='code,entity')
    assert got['cut_by'] == ['scene', 'sequence'] and got['omit'] == ['omt', 'cut'] and got['admins'] == ['Studio Admin', 'TD']
    assert got['groups'] == {'Lead': ['view', 'open']} and got['ips'] == '172.18.0.2' and got['guest'] == ['code']


def test_a_name_in_the_file_still_works_for_a_list(tmp_path):
    (tmp_path / 'c.toml').write_text('[access]\nadmin_group = "Studio Admin"\n[schema]\nomit_status = "cut"\n')
    got = read_config(tmp_path, REVIEWMEH_CONFIG=str(tmp_path / 'c.toml'))
    assert got['admins'] == ['Studio Admin'] and got['omit'] == ['cut']
