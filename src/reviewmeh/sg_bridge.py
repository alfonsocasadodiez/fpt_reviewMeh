#!/usr/bin/env python3
"""
ShotGrid access for the Review Tool.
shotgun_api3 is sync and its connections are not thread-safe → one connection per thread
(and per sudo login), always called from worker threads, never on the asyncio loop.
"""
import os
import time
import logging
import tempfile
import threading
from datetime import datetime

import shotgun_api3

from .config import SG_URL, SCRIPT_NAME, SCRIPT_KEY, PUBLIC_URL, APP_NAME, SCHEMA as S

logger = logging.getLogger(__name__)
_local = threading.local()

VERSION_FIELDS = ['code', 'entity', 'sg_task', 'user', 'sg_status_list', 'sg_first_frame',
                  'sg_uploaded_movie', 'sg_path_to_movie', 'project', 'created_at', 'image',
                  f'sg_task.Task.{S.task_order}']
# Lean fields for bulk lists (cut, compare): url fields are slow to sign; movie duration tells us there is a movie
LIST_FIELDS = ['code', 'entity', 'sg_task', 'user', 'sg_status_list', 'sg_first_frame', 'created_at',
               f'sg_task.Task.{S.task_order}', 'uploaded_movie_duration']


def _ts(dt):
    return dt.timestamp() if dt else None


def _cached(store, key, ttl, fn):
    hit = store.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    val = fn()
    store[key] = (time.time(), val)
    return val


def sg(sudo_login=None) -> shotgun_api3.Shotgun:
    cache = getattr(_local, 'conns', None)
    if cache is None:
        cache = _local.conns = {}
    if sudo_login not in cache:
        cache[sudo_login] = shotgun_api3.Shotgun(SG_URL, SCRIPT_NAME, SCRIPT_KEY,
                                                 sudo_as_login=sudo_login)
    return cache[sudo_login]


def version_info(v):
    entity = v.get('entity') or {}
    task = v.get('sg_task') or {}
    user = v.get('user') or {}
    return {
        'version_id': v['id'],
        'version_code': v.get('code') or f"Version {v['id']}",
        'entity_type': entity.get('type'),
        'entity_id': entity.get('id'),
        'entity_name': entity.get('name'),
        'task_id': task.get('id'),
        'task_name': task.get('name'),
        'artist_id': user.get('id') if user.get('type') == 'HumanUser' else None,
        'artist_name': user.get('name'),
        'sg_status': v.get('sg_status_list'),
        'first_frame': v.get('sg_first_frame') if v.get('sg_first_frame') is not None else 1,
        'project': v.get('project'),
        'task_order': v.get(f'sg_task.Task.{S.task_order}'),
        'created_at': _ts(v.get('created_at')),
        'image': v.get('image'),
        'has_movie': bool(v.get('sg_uploaded_movie') or v.get('sg_path_to_movie') or v.get('uploaded_movie_duration')),
    }


def fetch_versions(version_ids):
    if not version_ids:
        return []
    found = sg().find('Version', [['id', 'in', list(version_ids)]], VERSION_FIELDS)
    by_id = {v['id']: v for v in found}
    return [version_info(by_id[i]) for i in version_ids if i in by_id]


def fetch_playlist(playlist_id):
    pl = sg().find_one('Playlist', [['id', 'is', playlist_id]], ['code', 'project'])
    if not pl:
        return None, []
    conns = sg().find('PlaylistVersionConnection', [['playlist', 'is', {'type': 'Playlist', 'id': playlist_id}]],
                      ['version', 'sg_sort_order'],
                      order=[{'field_name': 'sg_sort_order', 'direction': 'asc'},
                             {'field_name': 'id', 'direction': 'asc'}])
    ids = [c['version']['id'] for c in conns if c.get('version')]
    return pl, ids


def search_versions(project_id, text='', episode_id=None, task=None, status=None, limit=60):
    filters = [['project', 'is', {'type': 'Project', 'id': project_id}]]
    if text:
        filters.append({'filter_operator': 'any', 'filters': [['code', 'contains', text],
                                                              ['entity.Shot.code', 'contains', text]]})
    level = project_cut(project_id) if episode_id else None
    if level:
        filters.append([f"entity.Shot.{level['field']}", 'is', {'type': level['type'], 'id': int(episode_id)}])
    if task:
        filters.append(['sg_task.Task.content', 'is', task])
    if status:
        filters.append(['sg_status_list', 'is', status])
    found = sg().find('Version', filters, VERSION_FIELDS,
                      order=[{'field_name': 'created_at', 'direction': 'desc'}], limit=limit)
    return [version_info(v) for v in found]


_filters_cache = {}


def project_filters(project_id):
    """What groups the project's shots (episodes, sequences or scenes) and task names, for the add-versions search."""
    def load():
        proj = {'type': 'Project', 'id': project_id}
        level = project_cut(project_id)
        eps = sg().find(level['type'], [['project', 'is', proj]], ['code'], order=[{'field_name': 'code', 'direction': 'asc'}]) if level else []
        groups = sg().summarize('Task', [['project', 'is', proj], ['entity', 'type_is', 'Shot']],
                                [{'field': S.task_order, 'type': 'minimum'}],
                                grouping=[{'field': 'content', 'type': 'exact', 'direction': 'asc'}])['groups']
        tasks = sorted(((g['summaries'].get(S.task_order) or 999, g['group_name']) for g in groups if g['group_name']))
        return {'episodes': [{'id': e['id'], 'code': e['code']} for e in eps], 'tasks': [t for _, t in tasks]}
    return _cached(_filters_cache, project_id, 600, load)


def entity_versions(entity_type, entity_id):
    """Every version of a shot/asset that has a movie, newest first (compare picker)."""
    found = sg().find('Version', [['entity', 'is', {'type': entity_type, 'id': int(entity_id)}]], LIST_FIELDS,
                      order=[{'field_name': 'created_at', 'direction': 'desc'}])
    return [vi for vi in map(version_info, found) if vi['has_movie']]


_episode_cache = {}
_cut_cache = {}


# Details panel: fields offered from the schema, minus what means nothing to read
_DETAIL_SKIP_TYPES = {'image', 'url', 'serializable', 'pivot_column', 'uuid', 'footage', 'password'}
MAX_LINK_HOPS = 3
_DETAIL_SKIP = {'cached_display_name', 'image_blur_hash', 'image_source_entity', 'filmstrip_image', 'open_notes', 'notes', 'tasks',
                'media_center_import_time', 'otio_playable', 'viewed_by_current_user', 'viewed_by_current_user_at', 'step_0'}
_fields_cache = {}


def entity_fields(entity_type):
    """Readable fields of an entity type. Single-entity fields carry 'links': the types they can point to."""
    def load():
        try:
            schema = sg().schema_field_read(entity_type)
        except shotgun_api3.Fault:
            return []  # a type this script key can't read
        rows = []
        for k, f in schema.items():
            kind = f['data_type']['value']
            if not f['visible']['value'] or k in _DETAIL_SKIP or kind in _DETAIL_SKIP_TYPES:
                continue
            row = {'key': k, 'name': f['name']['value'], 'type': kind}
            if kind == 'entity':
                row['links'] = (f['properties'].get('valid_types') or {}).get('value') or []
            rows.append(row)
        return sorted(rows, key=lambda r: r['name'].lower())
    return _cached(_fields_cache, entity_type, 3600, load)


def version_fields():
    return entity_fields('Version')


def _walk(parts):
    """Follow field, Type, field, Type… from Version. Returns (entity type reached, label parts); ValueError if invalid."""
    if len(parts) % 2 or len(parts) // 2 > MAX_LINK_HOPS:
        raise ValueError('Bad path')
    entity, labels = 'Version', []
    for i in range(0, len(parts), 2):
        f = next((x for x in entity_fields(entity) if x['key'] == parts[i]), None)
        if not f or parts[i + 1] not in f.get('links', []):
            raise ValueError('Bad path')
        labels.append(f['name'] + (f' ({parts[i + 1]})' if len(f['links']) > 1 else ''))
        entity = parts[i + 1]
    return entity, labels


def linked_fields(path):
    """The fields one link further along a path like 'entity.Shot' (Details picker)."""
    parts = path.split('.')
    entity, labels = _walk(parts)
    rows = []
    for f in entity_fields(entity):
        row = {**f, 'key': f'{path}.{f["key"]}', 'name': ' › '.join(labels + [f['name']])}
        if len(parts) // 2 >= MAX_LINK_HOPS:
            row.pop('links', None)
        rows.append(row)
    return rows


def field_defs(keys):
    """Definitions of the valid keys, plain or linked (entity.Shot.sg_cut_order); others are left out."""
    out = []
    for key in keys:
        parts = key.split('.')
        try:
            entity, labels = _walk(parts[:-1])
        except ValueError:
            continue
        f = next((x for x in entity_fields(entity) if x['key'] == parts[-1]), None)
        if f:
            out.append({'key': key, 'name': ' › '.join(labels + [f['name']]), 'type': f['type']})
    return out


def _value_out(v):
    if isinstance(v, datetime):
        return _ts(v)
    if isinstance(v, dict):
        return v.get('name') or v.get('code')
    if isinstance(v, list):
        return [_value_out(x) for x in v]
    return v


def version_details(version_id, keys):
    v = sg().find_one('Version', [['id', 'is', int(version_id)]], list(keys))
    return {k: _value_out(v.get(k)) for k in keys} if v else None


def versions_field(version_ids, key):
    """One field of many versions: {version_id: value}."""
    if not version_ids:
        return {}
    found = sg().find('Version', [['id', 'in', list(version_ids)]], [key])
    return {v['id']: _value_out(v.get(key)) for v in found}


def entity_notes(project_id, entity_type, entity_id):
    """ShotGrid Notes on a shot/asset or any of its versions, newest first; each with the version it is on."""
    ent = {'type': entity_type, 'id': int(entity_id)}
    versions = {v['id'] for v in sg().find('Version', [['entity', 'is', ent]], [])}
    links = [['note_links', 'is', ent]]
    if versions:
        links.append(['note_links', 'in', [{'type': 'Version', 'id': i} for i in versions]])
    rows = sg().find('Note', [['project', 'is', {'type': 'Project', 'id': int(project_id)}],
                              {'filter_operator': 'any', 'filters': links}],
                     ['content', 'subject', 'user', 'created_at', 'note_links', 'replies', 'sg_note_type', 'attachments'],
                     order=[{'field_name': 'created_at', 'direction': 'desc'}], limit=300)
    out = []
    for n in rows:
        ver = next((l for l in n.get('note_links') or [] if l['type'] == 'Version' and l['id'] in versions), None)
        user = n.get('user') or {}
        out.append({'id': n['id'], 'text': n.get('content') or n.get('subject') or '', 'author_name': user.get('name') or '',
                    'author_id': user.get('id') if user.get('type') == 'HumanUser' else None,
                    'created_at': _ts(n.get('created_at')), 'note_type': n.get('sg_note_type'),
                    'version_id': ver['id'] if ver else None, 'version_code': ver.get('name') if ver else None,
                    'images': [x['id'] for x in n.get('attachments') or [] if _is_image(x.get('name'))],
                    'replies': [r.get('name') or '' for r in n.get('replies') or []]})
    return out


def _is_image(name):
    return (name or '').lower().rsplit('.', 1)[-1] in ('png', 'jpg', 'jpeg')


def note_attachment_url(attachment_id, project_id):
    """Download url of an image attached to a Note in the project, else None."""
    a = sg().find_one('Attachment', [['id', 'is', int(attachment_id)], ['project', 'is', {'type': 'Project', 'id': int(project_id)}]],
                      ['this_file', 'filename', 'attachment_links'])
    if not a or not _is_image(a.get('filename')) or not any(l['type'] == 'Note' for l in a.get('attachment_links') or []):
        return None
    return (a.get('this_file') or {}).get('url')


def user_groups():
    """Every ShotGrid user: their permission group and whether the account is active."""
    rows = sg().find('HumanUser', [], ['permission_rule_set', 'sg_status_list'])
    return {str(u['id']): {'group': (u.get('permission_rule_set') or {}).get('name'), 'active': u.get('sg_status_list') == 'act'}
            for u in rows}


def user_image(user_id):
    return (sg().find_one('HumanUser', [['id', 'is', int(user_id)]], ['image']) or {}).get('image')


def shot_project_id(shot_id):
    def load():
        s = sg().find_one('Shot', [['id', 'is', int(shot_id)]], ['project'])
        return ((s or {}).get('project') or {}).get('id')
    return _cached(_episode_cache, ('project', shot_id), 3600, load)


def _link_field(entity, target):
    """The field on entity that links to target, if the site has one: its standard name, else any field linking only to target."""
    try:
        fields = sg().schema_field_read(entity)
    except shotgun_api3.Fault:
        return None  # that entity isn't enabled on this site
    found = [k for k, f in fields.items() if f.get('data_type', {}).get('value') == 'entity'
             and f.get('properties', {}).get('valid_types', {}).get('value') == [target]]
    standard = [k for k in (f'sg_{target.lower()}', target.lower()) if k in found]
    return (standard or sorted(found) or [None])[0]


def _paths_to(target):
    """
    Every path from a Shot to target, most direct first. An episode can be reached through the shot's sequence, and through
    its scene only if schema.cut_by asks for scenes: a link to a Scene alone doesn't mean scenes are part of the hierarchy.
    """
    paths = [p for p in [_link_field('Shot', target)] if p]
    if target == 'Episode':
        for parent in ('Sequence', 'Scene') if 'scene' in S.cut_by else ('Sequence',):
            hop, up = _link_field('Shot', parent), _link_field(parent, target)
            if hop and up:
                paths.append(f'{hop}.{parent}.{up}')
    return paths


def _entity_label(kind):
    """What this site calls the entity: Scene is often renamed."""
    try:
        return sg().schema_entity_read()[kind]['name']['value']
    except (shotgun_api3.Fault, KeyError):
        return kind


def cut_levels():
    """The ways this site can group shots into a cut, most wanted first: schema.cut_parent_field, or schema.cut_by found in its schema."""
    if S.cut_parent_field:
        return [{'type': S.cut_parent_type, 'field': S.cut_parent_field, 'label': S.cut_label or S.cut_parent_type}]

    def load():
        levels = []
        for name in S.cut_by:
            kind = name.capitalize()
            paths = _paths_to(kind)
            label = (S.cut_label or _entity_label(kind)) if paths else None
            levels += [{'type': kind, 'field': p, 'label': label} for p in paths]
        return levels
    return _cached(_episode_cache, 'levels', 600, load)


def project_cut(project_id):
    """What groups this project's shots: the first level with a shot linked to it, so a series and a film can share a site. Or None."""
    if S.cut_parent_field:
        return cut_levels()[0]
    if not project_id:
        return None

    def load():
        proj = {'type': 'Project', 'id': project_id}
        return next((lv for lv in cut_levels() if sg().find_one('Shot', [['project', 'is', proj], [lv['field'], 'is_not', None]], ['id'])), None)
    return _cached(_episode_cache, ('level', project_id), 600, load)


def shot_cut_parent(shot_id):
    """The shot's cut as (level, link to the entity that groups it), or None."""
    level = project_cut(shot_project_id(shot_id))
    if not level:
        return None

    def load():
        s = sg().find_one('Shot', [['id', 'is', int(shot_id)]], [level['field']])
        return (s or {}).get(level['field'])
    parent = _cached(_episode_cache, (level['field'], shot_id), 3600, load)
    return (level, parent) if parent else None


def load_cut(level, parent_id, fresh=False):
    """
    Shots of an episode, sequence or scene in cut order (omitted shots skipped), each with every version that has a movie,
    newest first, plus its tasks in pipeline order.
    """
    def load():
        link = {'type': level['type'], 'id': int(parent_id)}
        ep = sg().find_one(level['type'], [['id', 'is', int(parent_id)]], ['code'])
        filters = [[level['field'], 'is', link]]
        if S.omit_statuses:
            filters.append(['sg_status_list', 'not_in', S.omit_statuses])
        shots = sg().find('Shot', filters,
                          ['code', S.cut_order, S.cut_duration],
                          order=[{'field_name': S.cut_order, 'direction': 'asc'}, {'field_name': 'code', 'direction': 'asc'}])
        versions = sg().find('Version', [['entity', 'in', [{'type': 'Shot', 'id': s['id']} for s in shots]]] if shots else [['id', 'is', 0]],
                             LIST_FIELDS, order=[{'field_name': 'created_at', 'direction': 'desc'}])
        by_shot, tasks = {}, {}
        for vi in map(version_info, versions):
            if not vi['has_movie']:
                continue
            for k in ('project', 'image'):
                vi.pop(k, None)
            by_shot.setdefault(vi['entity_id'], []).append(vi)
            if vi['task_name']:
                tasks[vi['task_name']] = min(tasks.get(vi['task_name'], 9999), vi['task_order'] if vi['task_order'] is not None else 9999)
        return {
            'episode': {'id': int(parent_id), 'code': (ep or {}).get('code')},
            'tasks': [{'name': t, 'order': o} for t, o in sorted(tasks.items(), key=lambda x: (x[1], x[0]))],
            'shots': [{'id': s['id'], 'code': s['code'], 'cut_order': s.get(S.cut_order),
                       'cut_duration': s.get(S.cut_duration), 'versions': by_shot.get(s['id'], [])}
                      for s in shots if by_shot.get(s['id'])],
        }
    key = (level['type'], int(parent_id))
    if fresh:
        _cut_cache.pop(key, None)
    return _cached(_cut_cache, key, 120, load)


def forget_cuts():
    _cut_cache.clear()


def set_version_status(version_id, status, login=None):
    """Update a Version's status as the reviewer (falls back to the script user)."""
    try:
        sg(login).update('Version', int(version_id), {'sg_status_list': status})
    except shotgun_api3.Fault:
        if not login:
            raise
        sg().update('Version', int(version_id), {'sg_status_list': status})
    for cut in _cut_cache.values():
        for shot in cut[1]['shots']:
            for v in shot['versions']:
                if v['version_id'] == int(version_id):
                    v['sg_status'] = status


_users_cache = {}


def project_users(project_id):
    hit = _users_cache.get(project_id)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    filters = [['sg_status_list', 'is', 'act']]
    if project_id:
        filters.append(['projects', 'is', {'type': 'Project', 'id': project_id}])
    users = sg().find('HumanUser', filters, ['name', 'login'], order=[{'field_name': 'name', 'direction': 'asc'}])
    if not users and project_id:
        users = sg().find('HumanUser', [['sg_status_list', 'is', 'act']], ['name', 'login'],
                          order=[{'field_name': 'name', 'direction': 'asc'}])
    users = [{'id': u['id'], 'name': u['name'], 'login': u.get('login')} for u in users]
    _users_cache[project_id] = (time.time(), users)
    return users


# ─── sign in with ShotGrid (App Session Launcher, the flow Toolkit uses) ──────
# We ask the site for a session request, the user approves it in a ShotGrid tab, and polling then
# returns their login plus a session token. The token is used once to confirm the user and dropped.

def _asl(method, path, data=None):
    import json as _json
    import urllib.error
    import urllib.parse
    import urllib.request
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(SG_URL + path, data=body, method=method, headers={'User-Agent': 'ReviewMeh'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, _json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, None


def app_session_start():
    """→ {sessionRequestId, url}; the url is the approval page to open in the user's browser."""
    code, j = _asl('POST', '/internal_api/app_session_request', {'appName': APP_NAME, 'machineId': 'reviewmeh'})
    if code != 200 or not j or not j.get('sessionRequestId') or not j.get('url'):
        raise RuntimeError('ShotGrid sign-in is not available')
    return j


def app_session_poll(request_id):
    """→ None when declined or expired, {'approved': False} while waiting, else the approved user."""
    code, j = _asl('PUT', f'/internal_api/app_session_request/{request_id}')
    if code == 404:
        return None
    if code != 200 or not isinstance(j, dict):
        raise RuntimeError('ShotGrid did not answer')
    if not j.get('approved'):
        return {'approved': False}
    conn = shotgun_api3.Shotgun(SG_URL, session_token=j['sessionToken'])
    u = conn.find_one('HumanUser', [['login', 'is', j['userLogin']], ['sg_status_list', 'is', 'act']], ['name', 'login'])
    conn.close()
    if not u:
        raise RuntimeError('This ShotGrid account is not active')
    return {'approved': True, 'user': {'id': u['id'], 'name': u['name'], 'login': u['login']}}


def get_user(user_id):
    u = sg().find_one('HumanUser', [['id', 'is', int(user_id)]], ['name', 'login'])
    return {'id': u['id'], 'name': u['name'], 'login': u.get('login')} if u else None


_status_cache = {}


def _rgb_hex(value):
    """ShotGrid's '25,118,27' as '#19761b'."""
    try:
        return '#%02x%02x%02x' % tuple(int(x) for x in str(value).split(','))
    except (TypeError, ValueError):
        return None


def version_statuses(project_id=None):
    """The site's own Version statuses, as this project shows them. Re-read every few minutes, so a new status needs no restart."""
    def load():
        proj = {'type': 'Project', 'id': project_id} if project_id else None
        props = sg().schema_field_read('Version', 'sg_status_list', project_entity=proj)['sg_status_list']['properties']
        names = props.get('display_values', {}).get('value', {}) or {}
        colors = {s['code']: _rgb_hex(s.get('bg_color')) for s in sg().find('Status', [], ['code', 'bg_color'])}
        return [{'code': c, 'name': names.get(c, c), 'color': S.status_colors.get(c) or colors.get(c) or '#6e7681'}
                for c in props['valid_values']['value']]
    return _cached(_status_cache, project_id, 300, load)


def status_codes(entity):
    """The status codes this entity can have on this site; empty if it has no status field."""
    try:
        return sg().schema_field_read(entity, 'sg_status_list')['sg_status_list']['properties']['valid_values']['value']
    except shotgun_api3.Fault:
        return []


def note_status():
    """The status for new Notes. None when the site's Notes don't have it, as creating the Note would fail."""
    def load():
        return S.note_status if S.note_status in status_codes('Note') else None
    return _cached(_status_cache, 'note', 300, load) if S.note_status else None


def sudo_works():
    """Can the Script act as people? ShotGrid can block it per user, so try a read as a few recent ones. None: nobody to try."""
    users = sg().find('HumanUser', [['sg_status_list', 'is', 'act']], ['login'], order=[{'field_name': 'id', 'direction': 'desc'}], limit=5)
    for u in users:
        try:
            sg(u['login']).find_one('HumanUser', [['id', 'is', u['id']]], ['id'])
            return True
        except shotgun_api3.Fault:
            pass
    return False if users else None


def download_movie(version_id, dest_dir):
    """Return a local path to the version's movie (path_to_movie if reachable, else upload)."""
    v = sg().find_one('Version', [['id', 'is', version_id]], ['sg_uploaded_movie', 'sg_path_to_movie'])
    if not v:
        raise RuntimeError('Version not found in ShotGrid')
    p = v.get('sg_path_to_movie')
    if p and os.path.isfile(p):
        return p, os.path.basename(p), False
    up = v.get('sg_uploaded_movie')
    if not up:
        return None, None, False
    name = up.get('name') or f'{version_id}.mov'
    ext = os.path.splitext(name)[1] or '.mov'
    dest = os.path.join(dest_dir, f'src{ext}')
    sg().download_attachment(up, file_path=dest)
    return dest, name, True


# ─── publishing ───────────────────────────────────────────────────────────────

def note_end(n, notes, frame_count):
    """Last frame a top-level note covers. hold=-1 → until the frame before the next drawing."""
    last = max(0, (frame_count or 1) - 1)
    hold = n.get('hold') or 0
    if hold == -1:
        later = [m['frame'] for m in notes if m['parent_id'] is None and not m['deleted'] and m['shapes']
                 and m['frame'] > n['frame']]
        return max(n['frame'], min(later) - 1) if later else last
    return min(last, n['frame'] + hold) if hold > 0 else n['frame']


def frame_label(n, item, fps, end=None):
    """('f101' or 'f101-124', timecode of the first frame)."""
    frame = item['first_frame'] + n['frame']
    fps_i = max(1, round(fps or 24))
    secs, ff = divmod(n['frame'], fps_i)
    tc = f'{secs // 3600:02d}:{secs // 60 % 60:02d}:{secs % 60:02d}:{ff:02d}'
    label = f'f{frame}' if end is None or end <= n['frame'] else f"f{frame}-{item['first_frame'] + end}"
    return label, frame, tc


def pending_count(notes):
    return sum(1 for n in notes if not n['deleted'] and ((not n['sg_note_id'] and not n['sg_reply_id']) or n.get('sg_unsent')))


def _live_notes(ids):
    """Which of these ShotGrid notes still exist (people can delete them in ShotGrid)."""
    ids = list({i for i in ids if i})
    return {n['id'] for n in sg().find('Note', [['id', 'in', ids]], [])} if ids else set()


def publish_item(review, item, notes, opts, render_later, sync):
    """
    Push a review item's unsent notes to ShotGrid as one thread per version: the first note becomes the Note,
    with a link back to this version in the review; everything after it is a Reply, in frame order. ShotGrid only
    takes attachments on Notes and lists a thread by time, so each frame and file is uploaded right after its
    text, by its author (sudo when verified), and stays next to it. A second publish continues the same thread.
      sync.note(note_id, sg_note_id, sg_reply_id), sync.frame(note_id), sync.file(file_id, attachment_id): called as
      soon as each thing exists in ShotGrid, so a failure halfway resumes on retry and never duplicates.
      sync.path(file_id): where a file is on disk. render_later(item, note) -> Future of a PNG path: all frames start
      rendering up front, since the uploads have to go one by one to keep the thread in order.
    """
    fps = item.get('fps') or 24
    alive = [n for n in notes if not n['deleted']]
    by_id = {n['id']: n for n in alive}
    sent = {n['id']: n['sg_note_id'] for n in alive if n['sg_note_id']}
    live = _live_notes(sent.values())
    new_replies = sorted((n for n in alive if n['parent_id'] is not None and not n['sg_note_id'] and not n['sg_reply_id']), key=lambda n: n['created_at'])
    queue = []
    for n in sorted((n for n in alive if n['parent_id'] is None and not n['sg_note_id']), key=lambda n: (n['frame'], n['created_at'])):
        queue.append(n)
        queue += [r for r in new_replies if r['parent_id'] == n['id']]
    queued = {n['id'] for n in queue}
    queue += [r for r in new_replies if r['id'] not in queued and r['parent_id'] in by_id]  # replies to notes sent earlier
    thread = max((sent[k] for k in sent if sent[k] in live), default=None)
    attachments = created_notes = created_replies = 0
    last_write = [0.0]

    def write_gap():
        """ShotGrid orders a thread by creation time, in whole seconds: keep each step in a later second."""
        wait = 1.1 - (time.time() - last_write[0])
        if wait > 0:
            time.sleep(wait)
        last_write[0] = time.time()

    def wants_frame(n):
        return n['parent_id'] is None and not n.get('sg_frame_at') and bool(n['shapes'] or opts.get('attach_all_frames'))

    def attached(n):
        return bool(n['files'] or wants_frame(n))

    def as_author(n):
        verified = n['author_verified'] and n['author_login']
        return sg(n['author_login'] if verified else None), bool(verified)

    def upload_for(n, conn, note_id):
        nonlocal attachments
        def upload(path, display_name=None):
            try:
                return conn.upload('Note', note_id, path, display_name=display_name)
            except shotgun_api3.Fault:
                if conn is sg(None):
                    raise
                return sg().upload('Note', note_id, path, display_name=display_name)
        if wants_frame(n):
            label, frame, _ = frame_label(n, item, fps)
            tmpdir = tempfile.mkdtemp(prefix='review_pub_')
            try:
                dst = os.path.join(tmpdir, f"annot_version_{item['version_id']}.{frame}_{n['id']}.png")
                os.replace(renders.pop(n['id']).result(), dst)
                upload(dst)
                attachments += 1
                sync.frame(n['id'])
            finally:
                for f in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, f))
                os.rmdir(tmpdir)
        for f in n['files']:
            if not f['sg_attachment_id']:
                att = upload(sync.path(f['id']), f['name'])
                attachments += 1
                sync.file(f['id'], att)

    unfinished = [n for n in alive if n.get('sg_unsent') and n['sg_note_id'] in live]
    renders = {n['id']: render_later(item, n) for n in unfinished + queue if wants_frame(n)}
    try:
        # Frames and files a previous attempt couldn't deliver
        for n in unfinished:
            upload_for(n, as_author(n)[0], n['sg_note_id'])
        links = []
        if item['entity_type'] and item['entity_id']:
            links.append({'type': item['entity_type'], 'id': item['entity_id']})
        links.append({'type': 'Version', 'id': item['version_id']})
        if review.get('playlist_id'):
            links.append({'type': 'Playlist', 'id': review['playlist_id']})

        for n in queue:
            conn, verified = as_author(n)
            is_reply = n['parent_id'] is not None
            target = by_id.get(n['parent_id']) if is_reply else n
            label, _, tc = frame_label(target, item, fps, note_end(target, alive, item.get('frame_count')))
            text = n['text'].strip() or ('(attachment)' if n['files'] and not n['shapes'] else '(drawing)')
            line = f"{'Re ' if is_reply else ''}[{label} | {tc}] {text}"
            parent_note = sent.get(n['parent_id']) if is_reply else None
            reply_to = parent_note if parent_note in live else thread  # its note may have been deleted in ShotGrid
            if reply_to is None:
                head = [] if verified else [f"Review notes by {n['author_name']}", '']
                content = '\n'.join(head + [line, '', f"Open in {APP_NAME}: {PUBLIC_URL}/r/{review['token']}?item={item['id']}",
                                            f"— {review['name']}"])
                data = {
                    'project': {'type': 'Project', 'id': review['project_id']},
                    'subject': f"{review['name']}>{item['entity_name'] or ''}-{item['version_code']}",
                    'content': content,
                    'note_links': links,
                }
                if note_status():
                    data['sg_status_list'] = note_status()
                if item['task_id']:
                    data['tasks'] = [{'type': 'Task', 'id': item['task_id']}]
                if item['artist_id']:
                    data['addressings_to'] = [{'type': 'HumanUser', 'id': item['artist_id']}]
                if opts.get('note_type') and S.note_type:
                    data[S.note_type] = opts['note_type']
                try:
                    note = conn.create('Note', data)
                except shotgun_api3.Fault as e:
                    # sudo can fail for users without permission → fall back to the script user
                    if conn is sg(None):
                        raise
                    logger.warning('sudo create failed for %s (%s); retrying as script', n['author_login'], e)
                    data['content'] = f"Review notes by {n['author_name']}\n\n{content}"
                    conn = sg(None)
                    note = conn.create('Note', data)
                thread = note_id = note['id']
                live.add(note_id)
                last_write[0] = time.time()
                sync.note(n['id'], note_id, None)
                created_notes += 1
            else:
                write_gap()
                prefix = '' if verified else f"{n['author_name']}: "
                data = {'entity': {'type': 'Note', 'id': reply_to}, 'content': prefix + line}
                try:
                    reply = conn.create('Reply', data)
                except shotgun_api3.Fault as e:
                    if conn is sg(None):
                        raise
                    logger.warning('sudo reply failed for %s (%s); retrying as script', n['author_login'], e)
                    data['content'] = f"{n['author_name']}: {line}"
                    conn = sg(None)
                    reply = conn.create('Reply', data)
                note_id = reply_to
                sync.note(n['id'], note_id, reply['id'])
                created_replies += 1
            sent[n['id']] = note_id
            if attached(n):
                write_gap()
                upload_for(n, conn, note_id)
                last_write[0] = time.time()
    finally:
        for fut in renders.values():  # a failure halfway leaves frames nobody will upload
            fut.cancel()
            if not fut.cancelled() and not fut.exception():
                os.remove(fut.result())

    status = (opts.get('statuses') or {}).get(str(item['id']))
    if status:
        sg().update('Version', item['version_id'], {'sg_status_list': status})

    return {'item_id': item['id'], 'version_code': item['version_code'], 'notes': created_notes,
            'replies': created_replies, 'attachments': attachments, 'status': status}
