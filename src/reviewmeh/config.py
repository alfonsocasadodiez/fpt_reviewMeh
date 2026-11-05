"""
ReviewMeh configuration.

Read once at import from reviewmeh.toml (the path in REVIEWMEH_CONFIG, else the current folder). Every setting can
also come from an environment variable, which wins: the name in ENV below, else REVIEWMEH_<SECTION>_<KEY>. A container
needs no file at all. Lists are comma-separated; tables (access.groups, schema.status_colors) are JSON.
Run `reviewmeh init` to write a starting file and `reviewmeh check` to test it.
"""
import json
import os
import secrets
import tomllib
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.getenv('REVIEWMEH_CONFIG', 'reviewmeh.toml')).resolve()
_file = tomllib.loads(CONFIG_PATH.read_text()) if CONFIG_PATH.is_file() else {}

ENV = {
    ('shotgrid', 'url'): 'REVIEWMEH_SG_URL',
    ('shotgrid', 'script_name'): 'REVIEWMEH_SG_SCRIPT_NAME',
    ('shotgrid', 'script_key'): 'REVIEWMEH_SG_SCRIPT_KEY',
    ('server', 'public_url'): 'REVIEWMEH_PUBLIC_URL',
    ('server', 'secret'): 'REVIEWMEH_SECRET',
    ('server', 'host'): 'REVIEWMEH_HOST',
    ('server', 'port'): 'REVIEWMEH_PORT',
    ('storage', 'data_dir'): 'REVIEWMEH_DATA_DIR',
    ('hooks', 'ami_key'): 'REVIEWMEH_AMI_KEY',
    ('hooks', 'webhook_secret'): 'REVIEWMEH_WEBHOOK_SECRET',
    ('hooks', 'trust_ami_user'): 'REVIEWMEH_TRUST_AMI_USER',
    ('access', 'admin_group'): 'REVIEWMEH_ADMIN_GROUP',
    ('schema', 'cut_by'): 'REVIEWMEH_CUT_BY',
}


def _get(section, key, default=None):
    raw = os.getenv(ENV.get((section, key), f'REVIEWMEH_{section}_{key}'.upper()))
    if raw in (None, ''):
        return _file.get(section, {}).get(key, default)
    if isinstance(default, list):
        return _names(raw)
    return json.loads(raw) if isinstance(default, dict) else raw


def _names(value):
    """A list from a list, or from 'a, b' in the environment or a single name in the file."""
    return [x.strip() for x in value.split(',') if x.strip()] if isinstance(value, str) else list(value)


def _flag(section, key, default=False):
    v = _get(section, key, default)
    return v.strip().lower() in ('1', 'true', 'yes', 'on') if isinstance(v, str) else bool(v)


# ShotGrid
SG_URL = str(_get('shotgrid', 'url', '')).rstrip('/')
SCRIPT_NAME = _get('shotgrid', 'script_name', '')
SCRIPT_KEY = _get('shotgrid', 'script_key', '')

# Web server (single worker: websocket rooms and jobs live in memory)
PUBLIC_URL = str(_get('server', 'public_url', 'http://localhost:8086')).rstrip('/')
HOST = _get('server', 'host', '127.0.0.1')
PORT = int(_get('server', 'port', 8086))
FORWARDED_ALLOW_IPS = _get('server', 'forwarded_allow_ips', '127.0.0.1')

# Storage. The database must be on a local disk (network shares break SQLite locking); media can be on a NAS.
DATA_DIR = Path(_get('storage', 'data_dir', 'data')).resolve()
DB_PATH = str(Path(_get('storage', 'db', DATA_DIR / 'reviewmeh.db')))
MEDIA_ROOT = str(Path(_get('storage', 'media_root', DATA_DIR / 'media')))
BACKUP_DIR = str(Path(_get('storage', 'backup_dir', DATA_DIR / 'backups')))
BACKUP_KEEP = int(_get('storage', 'backup_keep', 30))
CACHE_DAYS = int(_get('storage', 'cache_days', 15))  # Delete proxies and other copies of ShotGrid's files after this many days unused. 0: keep them


def _made_once(name):
    """A secret nobody has to handle: created on first use and kept with the data, so it survives restarts and new images."""
    folder, path = DATA_DIR / 'secrets', DATA_DIR / 'secrets' / name
    try:
        if not path.is_file():
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            tmp = folder / f'.{name}.{os.getpid()}'
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
                f.write(secrets.token_urlsafe(32))
            try:
                os.link(tmp, path)  # atomic: if two processes start together, the first one wins and both read its value
            except FileExistsError:
                pass
            finally:
                tmp.unlink()
        return path.read_text().strip()
    except OSError:
        return ''  # problems() reports it


SECRET = _get('server', 'secret', '') or _made_once('server_secret')

# Look
APP_NAME = _get('branding', 'app_name', 'ReviewMeh')
LOGO_PATH = str(_get('branding', 'logo', '') or HERE / 'static' / 'logo.svg')
FAVICON_PATH = str(_get('branding', 'favicon', '') or _get('branding', 'logo', '') or HERE / 'static' / 'favicon.svg')
ACCENT = _get('branding', 'accent', '#0796d4')
ACCENT_HOVER = _get('branding', 'accent_hover', '#0aa6eb')

# Media
PROXY_MAX_WIDTH = int(_get('media', 'proxy_max_width', 1920))
PROXY_CRF = int(_get('media', 'proxy_crf', 18))
INGEST_WORKERS = int(_get('media', 'ingest_workers', 4))
RENDER_WORKERS = int(_get('media', 'render_workers', 8))
PUBLISH_VERSIONS = int(_get('media', 'publish_versions', 4))
FONT_PATH = _get('media', 'font', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf')

# Coming in from ShotGrid
AMI_KEY = _get('hooks', 'ami_key', '') or _made_once('ami_key')
WEBHOOK_SECRET = _get('hooks', 'webhook_secret', '') or _made_once('webhook_secret')
# ShotGrid can't prove who clicked an Action Menu Item. Off: people sign in with ShotGrid. On: the AMI's user_id signs them in
# with no further check, so anyone holding ami_key can become any user. Only for sites where the App Session Launcher is off.
TRUST_AMI_USER = _flag('hooks', 'trust_ami_user', False)

# Who can do what. The Access tab on the dashboard can change these later: its choices win over this file.
ADMIN_GROUPS = set(_names(_get('access', 'admin_group', ['Admin'])))  # always every right, and they edit the rest; a group not listed has none
GROUP_RIGHTS = _get('access', 'groups', {
    'Manager': ['view', 'open', 'comment', 'download', 'versions', 'status', 'publish', 'add', 'present', 'share'],
    'Artist': ['view', 'open', 'comment', 'download', 'versions', 'present'],
})
# Readable by people outside the studio (guests, groups without All reviews)
GUEST_FIELDS = set(_get('access', 'guest_fields', ['code', 'entity', 'sg_task', 'user', 'sg_status_list', 'description', 'frame_range', 'frame_count',
                                                  'sg_first_frame', 'sg_last_frame', 'sg_uploaded_movie_frame_rate', 'created_at', 'updated_at', 'sg_version_type']))
DETAIL_FIELDS = _get('access', 'detail_fields', ['entity', 'sg_task', 'user', 'sg_status_list', 'description', 'frame_range', 'created_at', 'created_by'])
SHARE_DEFAULT_DAYS = int(_get('access', 'share_default_days', 7))
SHARE_MAX_DAYS = int(_get('access', 'share_max_days', 30))
MAX_UPLOAD_MB = int(_get('access', 'max_upload_mb', 500))
MAX_GUEST_UPLOAD_MB = int(_get('access', 'max_guest_upload_mb', 50))

# ShotGrid fields and statuses that differ between sites. `reviewmeh check` says if one is missing.
SCHEMA = SimpleNamespace(
    task_order=_get('schema', 'task_order', 'sg_sort_order'),            # Task: pipeline order
    cut_order=_get('schema', 'cut_order', 'sg_cut_order'),               # Shot: position in the cut
    cut_duration=_get('schema', 'cut_duration', 'sg_cut_duration'),      # Shot: frames, used until a movie is loaded
    omit_statuses=_names(_get('schema', 'omit_status', ['omt'])),        # Shot statuses skipped in cuts
    note_status=_get('schema', 'note_status', 'opn'),                    # Status of new Notes, if the site's Notes have it. Empty: the site's default
    # What groups shots into a cut: episode, sequence, scene. Each project uses the first of these its shots are linked to.
    # Scenes only when asked for, as a link to a Scene doesn't mean scenes are part of the hierarchy. "off": no Mini-cut or cut view.
    cut_by=[n.lower() for n in _names(_get('schema', 'cut_by', ['episode', 'sequence']))],
    cut_label=_get('schema', 'cut_label', ''),                           # Name shown for it. Empty: the entity's name
    # Only for something cut_by can't find, such as a custom entity. Replaces cut_by for the whole site.
    cut_parent_type=_get('schema', 'cut_parent_type', ''),
    cut_parent_field=_get('schema', 'cut_parent_field', ''),
    note_type=_get('schema', 'note_type', ''),                           # Note list field for types. Empty: no type picker
    note_types=_get('schema', 'note_types', []),
    status_colors=_get('schema', 'status_colors', {}),                   # Overrides of the colours ShotGrid has for statuses: {code = "#rrggbb"}
)


def problems():
    """What stops the server from running."""
    out = []
    for name, value, how in (('shotgrid.url', SG_URL, 'your site address'), ('shotgrid.script_name', SCRIPT_NAME, 'a Script in ShotGrid'),
                             ('shotgrid.script_key', SCRIPT_KEY, "that Script's key"), ('server.secret', SECRET, f'made automatically, but {DATA_DIR} could not be written')):
        if not value:
            out.append(f'{name} is not set: {how}')
    return out


def public():
    """What the browser may know. Nothing secret."""
    return {'app_name': APP_NAME, 'sg_url': SG_URL, 'accent': ACCENT, 'accent_hover': ACCENT_HOVER, 'note_types': SCHEMA.note_types if SCHEMA.note_type else [], 'detail_fields': DETAIL_FIELDS,
            'share_default_days': SHARE_DEFAULT_DAYS, 'share_max_days': SHARE_MAX_DAYS, 'max_guest_upload_mb': MAX_GUEST_UPLOAD_MB}
