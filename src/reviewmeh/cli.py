"""The `reviewmeh` command: init, check, setup-amis, webhook, serve."""
import argparse
import importlib.util
import os
import secrets
import shutil
import sys
from pathlib import Path


def cmd_init(args):
    dest = Path(args.path)
    if dest.exists():
        sys.exit(f'{dest} already exists')
    template = (Path(__file__).parent / 'reviewmeh.example.toml').read_text()
    for name in ('SECRET', 'AMI_KEY', 'WEBHOOK_SECRET'):
        template = template.replace(f'__{name}__', secrets.token_urlsafe(32))
    dest.write_text(template)
    dest.chmod(0o600)
    print(f'Wrote {dest}. Fill in [shotgrid] and server.public_url, then run: reviewmeh check')


def cmd_check(_):
    from . import config
    ok = True

    def line(good, text, warn=False):
        nonlocal ok
        print(f"{'ok  ' if good else 'warn' if warn else 'FAIL'}  {text}")
        ok = ok and (good or warn)

    print(f'Config: {config.CONFIG_PATH}' + ('' if config.CONFIG_PATH.is_file() else ' (not found, using environment only)'))
    for p in config.problems():
        line(False, p)
    for tool in ('ffmpeg', 'ffprobe'):
        line(bool(shutil.which(tool)), f'{tool} on PATH')
    line(importlib.util.find_spec('weasyprint') is not None, 'weasyprint installed, for PDF reports (pip install "reviewmeh[pdf]")', warn=True)
    line(os.path.isfile(config.FONT_PATH), f'font for captions: {config.FONT_PATH}', warn=True)
    line(config.PUBLIC_URL.startswith('https://'), f'public_url {config.PUBLIC_URL}' + ('' if config.PUBLIC_URL.startswith('https://') else ': use https, or sign-in cookies travel unprotected'), warn=True)
    line(not config.TRUST_AMI_USER, 'hooks.trust_ami_user is off' if not config.TRUST_AMI_USER else 'hooks.trust_ami_user is ON: anyone with ami_key can sign in as any user', warn=True)
    for folder in (Path(config.DB_PATH).parent, Path(config.MEDIA_ROOT), Path(config.BACKUP_DIR)):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            probe = folder / '.reviewmeh_check'
            probe.write_text('x')
            probe.unlink()
            line(True, f'can write to {folder}')
        except OSError as e:
            line(False, f'can write to {folder}: {e}')
    if config.problems():
        sys.exit(1)

    from . import sg_bridge
    try:
        sg = sg_bridge.sg()
        info = sg.info()
        line(True, f'connected to {config.SG_URL} (ShotGrid {".".join(map(str, info.get("version", [])[:3]))})')
    except Exception as e:
        line(False, f'connect to {config.SG_URL}: {e}')
        sys.exit(1)
    line(bool(info.get('authentication_app_session_launcher_enabled')), 'App Session Launcher enabled, needed for "Sign in with ShotGrid"', warn=config.TRUST_AMI_USER)
    S = config.SCHEMA
    can_sudo = sg_bridge.sudo_works()
    line(can_sudo is not False, 'notes can appear under the person who wrote them' if can_sudo is not False
         else "the Script can't act as people, so notes come from it with the author's name in the text. To fix, turn on "
         "Advanced > Perform actions as though logged in as another user in its permission group", warn=True)

    def has(entity, field):
        try:
            return field in sg.schema_field_read(entity)
        except Exception:
            return False
    for entity, field in [('Task', S.task_order), ('Shot', S.cut_order), ('Shot', S.cut_duration)] + ([('Note', S.note_type)] if S.note_type else []):
        line(has(entity, field), f'{entity}.{field} exists', warn=True)
    if S.cut_parent_field:
        first = S.cut_parent_field.split('.')[0]
        line(has('Shot', first), f'Shot.{first} exists (cut view: {S.cut_parent_field})', warn=True)
    elif S.cut_by == ['off']:
        print('      cut view off: schema.cut_by is "off"')
    else:
        levels = sg_bridge.cut_levels()
        line(bool(levels), f"cuts can be grouped by {', '.join(S.cut_by)}: each project uses the first its shots are linked to" if levels
             else f"Shot has no link to any of: {', '.join(S.cut_by)}. Mini-cut and the cut view stay off", warn=True)
        for lv in levels:
            print(f"        {lv['label']}: Shot.{lv['field']}")
    shot_codes = sg_bridge.status_codes('Shot')
    for code in S.omit_statuses:
        line(code in shot_codes, f'Shot status "{code}" exists, left out of cuts', warn=True)
    if S.note_status:
        line(bool(sg_bridge.note_status()), f'Note status "{S.note_status}" exists, given to new Notes' if sg_bridge.note_status()
             else f'Note status "{S.note_status}" does not exist: new Notes get the site default', warn=True)
    print('      Version statuses: ' + ', '.join(s['name'] for s in sg_bridge.version_statuses()))
    _report_groups(config, sg_bridge, line)
    print('All good.' if ok else 'Fix the FAIL lines above.')
    sys.exit(0 if ok else 1)


def _report_groups(config, sg_bridge, line):
    """Each ShotGrid permission group in use and what it gets here: the file's rights, then the Access tab's changes."""
    from . import db
    try:
        rights = config.GROUP_RIGHTS | db.get_setting('group_rights', {})
    except Exception:  # no database until the server has started once
        rights = config.GROUP_RIGHTS
    people = {}
    for u in sg_bridge.user_groups().values():
        if u['active'] and u['group']:
            people[u['group']] = people.get(u['group'], 0) + 1
    admins = config.ADMIN_GROUPS & set(people)
    line(bool(admins), f"admin group {', '.join(sorted(admins))} can open the Access tab" if admins
         else f"nobody is in the admin group {', '.join(sorted(config.ADMIN_GROUPS))}: set access.admin_group (REVIEWMEH_ADMIN_GROUP) to a group below")
    print('      Permission groups:')
    for group, count in sorted(people.items()):
        what = 'every right' if group in config.ADMIN_GROUPS else ', '.join(rights.get(group, [])) or 'no access yet (set it on the Access tab)'
        print(f'        {group} ({count}): {what}')


AMIS = [('Send to Review', 'review', ('Playlist', 'Version')), ('Publish Review Notes', 'publish', ('Playlist', 'Version')),
        ('Export Review Notes', 'export', ('Playlist', 'Version'))]


def cmd_setup_amis(args):
    from . import config, sg_bridge
    if not config.AMI_KEY:
        sys.exit(f'No hooks.ami_key: could not create one in {config.DATA_DIR}')
    sg = sg_bridge.sg()
    for title, action, entities in AMIS:
        url = f'{config.PUBLIC_URL}/ami/{action}?key={config.AMI_KEY}'
        for entity in entities:
            found = sg.find_one('ActionMenuItem', [['title', 'is', title], ['entity_type', 'is', entity]], ['url'])
            if found and found.get('url') == url:
                print(f'exists   {title} on {entity}')
            elif not args.apply:
                print(f"would {'update' if found else 'create'}  {title} on {entity}")
            elif found:
                sg.update('ActionMenuItem', found['id'], {'url': url})
                print(f'updated  {title} on {entity}')
            else:
                sg.create('ActionMenuItem', {'title': title, 'url': url, 'entity_type': entity, 'selection_required': True})
                print(f'created  {title} on {entity}')
    if not args.apply:
        print('Nothing changed. Run again with --apply to write this to ShotGrid.')


def cmd_webhook(_):
    from . import config
    if not config.WEBHOOK_SECRET:
        sys.exit(f'No hooks.webhook_secret: could not create one in {config.DATA_DIR}')
    S = config.SCHEMA
    print('Optional, for live updates. In ShotGrid, open Webhooks and add one with:\n')
    print(f'  URL:           {config.PUBLIC_URL}/hooks/shotgrid')
    print(f'  Secret Token:  {config.WEBHOOK_SECRET}')
    print('  Events:        Version changes to Status, Uploaded Movie and Path to Movie')
    print(f'                 Shot changes to Status, {S.cut_order} and {S.cut_duration}')


def cmd_serve(_):
    from . import config
    if config.problems():
        sys.exit('\n'.join(config.problems()) + '\nRun: reviewmeh check')
    import uvicorn
    uvicorn.run('reviewmeh.server:app', host=config.HOST, port=config.PORT, workers=1, proxy_headers=True, forwarded_allow_ips=config.FORWARDED_ALLOW_IPS)


def main():
    parser = argparse.ArgumentParser(prog='reviewmeh', description='Frame-accurate review of ShotGrid Versions.')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('init', help='write a starting reviewmeh.toml with fresh secrets')
    p.add_argument('path', nargs='?', default='reviewmeh.toml')
    p.set_defaults(fn=cmd_init)
    sub.add_parser('check', help='test the configuration, tools and ShotGrid connection').set_defaults(fn=cmd_check)
    p = sub.add_parser('setup-amis', help='create the Action Menu Items in ShotGrid (shows what it would do unless --apply)')
    p.add_argument('--apply', action='store_true')
    p.set_defaults(fn=cmd_setup_amis)
    sub.add_parser('webhook', help='print what to enter in ShotGrid for live updates').set_defaults(fn=cmd_webhook)
    sub.add_parser('serve', help='run the server').set_defaults(fn=cmd_serve)
    args = parser.parse_args()
    args.fn(args)


if __name__ == '__main__':
    main()
