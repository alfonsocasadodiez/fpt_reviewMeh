"""A small stand-in for shotgun_api3.Shotgun. Writes are recorded in .calls, in order."""
import os

import shotgun_api3

USERS = {1: ('Admin', 'act', 'Ada'), 2: ('Artist', 'act', 'Art'), 3: ('Manager', 'act', 'Max'), 4: ('Artist', 'dis', 'Gone'), 5: ('Client', 'act', 'Cleo')}
VERSIONS = {
    100: {'code': 'sh010_anim_v001', 'entity': {'type': 'Shot', 'id': 10, 'name': 'sh010'}, 'project': {'type': 'Project', 'id': 7}},
    101: {'code': 'sh020_anim_v001', 'entity': {'type': 'Shot', 'id': 20, 'name': 'sh020'}, 'project': {'type': 'Project', 'id': 7}},
    200: {'code': 'other_project_v001', 'entity': {'type': 'Shot', 'id': 30, 'name': 'x'}, 'project': {'type': 'Project', 'id': 8}},
}


def same(value, wanted):
    """Links match on type and id, as in ShotGrid; a row's link may carry a name too."""
    if isinstance(value, dict) and isinstance(wanted, dict):
        return (value['type'], value['id']) == (wanted['type'], wanted['id'])
    return value == wanted


class Conn:
    def __init__(self, world, sudo):
        self.world, self.sudo = world, sudo

    def _next(self):
        self.world.next_id += 1
        return self.world.next_id

    def create(self, entity, data, return_fields=None):
        if self.sudo in self.world.refuse_sudo:
            raise shotgun_api3.Fault('sudo not allowed')
        new = self._next()
        self.world.calls.append({'kind': 'create', 'entity': entity, 'id': new, 'data': data, 'sudo': self.sudo})
        if entity == 'Note':
            self.world.notes.add(new)
        return {'type': entity, 'id': new}

    def upload(self, entity, entity_id, path, field_name=None, display_name=None, tag_list=None):
        name = os.path.basename(path)
        if name in self.world.fail_uploads:
            self.world.fail_uploads.remove(name)
            raise RuntimeError('upload failed')
        new = self._next()
        self.world.calls.append({'kind': 'upload', 'entity': entity, 'entity_id': entity_id, 'file': name, 'display_name': display_name, 'sudo': self.sudo})
        return new

    def update(self, entity, entity_id, data, **kw):
        self.world.calls.append({'kind': 'update', 'entity': entity, 'entity_id': entity_id, 'data': data})
        return {'type': entity, 'id': entity_id, **data}

    def info(self):
        return {'version': [8, 0, 0], 'authentication_app_session_launcher_enabled': True}

    def summarize(self, *a, **kw):
        return {'groups': []}

    def schema_entity_read(self):
        return {k: {'name': {'value': v}} for k, v in self.world.entity_names.items()}

    def find(self, entity, filters, fields=None, **kw):
        if self.sudo in self.world.refuse_sudo:
            raise shotgun_api3.Fault('sudo not allowed')
        if entity == 'Shot':
            rows = self.world.shots
            for field, op, wanted in filters:
                if op == 'is':
                    rows = [r for r in rows if same(r.get(field), wanted)]
                elif op == 'is_not':
                    rows = [r for r in rows if not same(r.get(field), wanted)]
                elif op == 'not_in':
                    rows = [r for r in rows if r.get(field) not in wanted]
            return sorted(rows, key=lambda r: r.get('sg_cut_order', 0))
        if entity in self.world.parents:
            one = next((f[2] for f in filters if f[:2] == ['id', 'is']), None)
            return [r for r in self.world.parents[entity] if one is None or r['id'] == one]
        if entity == 'HumanUser':
            rows = [{'type': 'HumanUser', 'id': i, 'name': n, 'login': f'{n.lower()}@test', 'permission_rule_set': {'name': g}, 'sg_status_list': s, 'image': None}
                    for i, (g, s, n) in USERS.items()]
            for f in filters:
                if f[:2] == ['id', 'is']:
                    rows = [r for r in rows if r['id'] == f[2]]
            return rows
        if entity == 'Note':
            ids = next((f[2] for f in filters if isinstance(f, list) and f[:2] == ['id', 'in']), [])
            return [{'type': 'Note', 'id': i} for i in ids if i in self.world.notes]
        if entity == 'Status':
            return [{'code': 'rev', 'bg_color': '192,97,253'}, {'code': 'apr', 'bg_color': '25,118,27'}]
        if entity == 'Version':
            ids = next((f[2] for f in filters if isinstance(f, list) and f[:2] == ['id', 'in']), None)
            one = next((f[2] for f in filters if isinstance(f, list) and f[:2] == ['id', 'is']), None)
            wanted = ids if ids is not None else [one] if one else list(VERSIONS)
            return [{'type': 'Version', 'id': i, 'sg_status_list': 'rev', 'sg_uploaded_movie': {'url': 'x'}, **VERSIONS[i]} for i in wanted if i in VERSIONS]
        if entity == 'Playlist':
            return [{'type': 'Playlist', 'id': 50, 'code': 'Dailies'}]
        if entity == 'PlaylistVersionConnection':
            return [{'version': {'type': 'Version', 'id': 100}}, {'version': {'type': 'Version', 'id': 101}}]
        return []

    def find_one(self, entity, filters, fields=None, **kw):
        rows = self.find(entity, filters, fields, **kw)
        return rows[0] if rows else None

    def schema_field_read(self, entity, field=None, project_entity=None):
        if field == 'sg_status_list':
            if entity not in self.world.statuses:
                raise shotgun_api3.Fault('no such field')
            codes = self.world.statuses[entity]
            if entity == 'Version' and project_entity:
                codes = self.world.project_statuses.get(project_entity['id'], codes)
            return {field: {'properties': {'valid_values': {'value': codes}, 'display_values': {'value': {'rev': 'Review', 'apr': 'Approved', 'hld': 'Hold'}}}}}
        common = {'name': {'value': 'x'}, 'visible': {'value': True}, 'properties': {}}
        tables = {
            'Version': {'code': 'text', 'entity': 'entity', 'description': 'text', 'sg_path_to_movie': 'text', 'sg_task': 'entity', 'sg_status_list': 'status_list'},
            'Shot': {'code': 'text', 'sg_cut_order': 'number'},
            'Task': {'content': 'text'},
        }
        out = {}
        for k, kind in tables.get(entity, {}).items():
            out[k] = {**common, 'name': {'value': k.replace('sg_', '').title()}, 'data_type': {'value': kind}}
            if kind == 'entity':
                out[k]['properties'] = {'valid_types': {'value': {'entity': ['Shot', 'Asset'], 'sg_task': ['Task']}[k]}}
        for k, target in self.world.links.get(entity, {}).items():
            out[k] = {**common, 'data_type': {'value': 'entity'}, 'properties': {'valid_types': {'value': [target]}}}
        return out


class FakeShotgrid:
    def __init__(self):
        self.calls, self.notes, self.fail_uploads, self.refuse_sudo = [], set(), [], set()
        self.next_id = 90000
        self.shots, self.parents, self.links, self.entity_names = [], {}, {}, {}  # a site's own cut setup
        self.statuses = {'Version': ['rev', 'apr', 'hld'], 'Note': ['opn', 'clsd'], 'Shot': ['ip', 'omt', 'cut']}
        self.project_statuses = {}
        self._conns = {}

    def as_user(self, login=None):
        return self._conns.setdefault(login, Conn(self, login))

    def writes(self, *kinds):
        return [c for c in self.calls if not kinds or c['kind'] in kinds]
