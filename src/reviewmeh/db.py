#!/usr/bin/env python3
"""
SQLite persistence: reviews, items, media, notes (drawings live on notes as JSON) and drafts.

Durability: WAL + synchronous=FULL, so a note is on disk before the POST returns.
backup() writes a consistent copy (online backup API) to BACKUP_DIR; the server runs it daily.
"""
import os
import json
import time
import sqlite3
import threading
from datetime import datetime

from .config import DB_PATH, BACKUP_DIR, BACKUP_KEEP

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    project_id INTEGER,
    project_name TEXT,
    playlist_id INTEGER,
    created_by_id INTEGER,
    created_by_name TEXT,
    created_at REAL NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reviews_playlist ON reviews(playlist_id);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    position INTEGER NOT NULL DEFAULT 0,
    version_id INTEGER NOT NULL,
    version_code TEXT,
    entity_type TEXT,
    entity_id INTEGER,
    entity_name TEXT,
    task_id INTEGER,
    task_name TEXT,
    artist_id INTEGER,
    artist_name TEXT,
    sg_status TEXT,
    first_frame INTEGER NOT NULL DEFAULT 1,
    added_at REAL NOT NULL,
    UNIQUE(review_id, version_id)
);

CREATE TABLE IF NOT EXISTS media (
    version_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'queued',   -- queued|downloading|transcoding|ready|error|nomedia|cleared (removed while unused; rebuilt when needed)
    progress REAL NOT NULL DEFAULT 0,
    error TEXT,
    src_name TEXT,
    fps REAL,
    frame_count INTEGER,
    width INTEGER,
    height INTEGER,
    has_audio INTEGER NOT NULL DEFAULT 0,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id INTEGER NOT NULL REFERENCES items(id),
    parent_id INTEGER REFERENCES notes(id),
    author_id INTEGER,
    author_name TEXT NOT NULL,
    author_login TEXT,
    author_verified INTEGER NOT NULL DEFAULT 0,
    frame INTEGER NOT NULL DEFAULT 0,      -- 0-based index into the proxy
    hold INTEGER NOT NULL DEFAULT 0,       -- note covers frame..frame+hold; -1 = until the next drawing
    text TEXT NOT NULL DEFAULT '',
    shapes TEXT,                           -- JSON list of normalized vector shapes
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,
    sg_note_id INTEGER,
    sg_reply_id INTEGER,
    synced_at REAL
);
CREATE INDEX IF NOT EXISTS idx_notes_item ON notes(item_id);

-- Unposted drawings and comment text, one blob per reviewer per review (mirrors the browser's copy)
CREATE TABLE IF NOT EXISTS drafts (
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    user_key TEXT NOT NULL,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (review_id, user_key)
);

-- Share links: what people outside the studio link can do, per role, until the link expires
CREATE TABLE IF NOT EXISTS shares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    key TEXT UNIQUE NOT NULL,
    perms TEXT NOT NULL,                   -- JSON: what guests may do {"comment", "download", "versions", "details"}
    created_by_name TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL
);
CREATE INDEX IF NOT EXISTS idx_shares_review ON shares(review_id);

-- Files people attach to notes. note_id stays NULL until the note is posted.
CREATE TABLE IF NOT EXISTS note_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id INTEGER NOT NULL REFERENCES reviews(id),
    note_id INTEGER REFERENCES notes(id),
    user_key TEXT NOT NULL,
    name TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    sg_attachment_id INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_note_files_note ON note_files(note_id);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL                    -- JSON
);
"""

TOKEN_BYTES = 32  # review tokens and share keys: 43 url-safe characters

# Additive migrations: (table, column, DDL). Never drop or rename.
MIGRATIONS = [
    ('reviews', 'playlist_name', 'ALTER TABLE reviews ADD COLUMN playlist_name TEXT'),
    ('notes', 'client_id', 'ALTER TABLE notes ADD COLUMN client_id TEXT'),
    ('shares', 'guests', 'ALTER TABLE shares ADD COLUMN guests INTEGER NOT NULL DEFAULT 0'),
    ('shares', 'password_hash', 'ALTER TABLE shares ADD COLUMN password_hash TEXT'),
    ('notes', 'sg_frame_at', 'ALTER TABLE notes ADD COLUMN sg_frame_at REAL'),  # when its annotated frame reached ShotGrid
    ('media', 'last_used', 'ALTER TABLE media ADD COLUMN last_used REAL'),  # when its proxy was last needed
    ('media', 'built_at', 'ALTER TABLE media ADD COLUMN built_at REAL'),  # when its proxy was made: part of its url, so browsers never play an old one
]


def conn() -> sqlite3.Connection:
    c = getattr(_local, 'conn', None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA synchronous=FULL')
        c.execute('PRAGMA foreign_keys=ON')
        _local.conn = c
    return c


def init():
    c = conn()
    c.executescript(SCHEMA)
    first_frames = 'sg_frame_at' not in {r['name'] for r in c.execute('PRAGMA table_info(notes)')}
    for table, col, ddl in MIGRATIONS:
        if col not in {r['name'] for r in c.execute(f'PRAGMA table_info({table})')}:
            c.execute(ddl)
    if first_frames:  # everything published so far already carries its frame
        c.execute('UPDATE notes SET sg_frame_at=synced_at WHERE synced_at IS NOT NULL')
    # Proxies from before last_used existed: their cleanup period starts now
    c.execute('UPDATE media SET last_used=? WHERE last_used IS NULL', (time.time(),))
    # Idempotent posts: the browser retries with the same client_id until it gets an answer
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_client ON notes(client_id) WHERE client_id IS NOT NULL')


def backup():
    """Consistent copy of the live DB into BACKUP_DIR; keeps the newest BACKUP_KEEP files."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f'review-{datetime.now():%Y%m%d-%H%M}.db')
    tmp = dest + '.tmp'
    out = sqlite3.connect(tmp)
    try:
        conn().backup(out)
    finally:
        out.close()
    os.replace(tmp, dest)
    olds = sorted(f for f in os.listdir(BACKUP_DIR) if f.startswith('review-') and f.endswith('.db'))
    for f in olds[:-BACKUP_KEEP]:
        os.remove(os.path.join(BACKUP_DIR, f))
    return dest


def q(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def q1(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def ex(sql, args=()):
    cur = conn().execute(sql, args)
    return cur.lastrowid


# ─── reviews ──────────────────────────────────────────────────────────────────

def review_by_token(token):
    return q1('SELECT * FROM reviews WHERE token=?', (token,))


def review(review_id):
    return q1('SELECT * FROM reviews WHERE id=?', (review_id,))


def share_row(s):
    if s:
        s['perms'] = json.loads(s['perms'])
    return s


def get_setting(key, default=None):
    r = q1('SELECT value FROM settings WHERE key=?', (key,))
    return json.loads(r['value']) if r else default


def put_setting(key, value):
    ex('INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
       (key, json.dumps(value)))


def share_by_key(key):
    return share_row(q1('SELECT * FROM shares WHERE key=?', (key,)))


def review_shares(review_id, now):
    return [share_row(s) for s in q('SELECT * FROM shares WHERE review_id=? AND revoked_at IS NULL AND expires_at>? '
                                     'ORDER BY created_at DESC', (review_id, now))]


def review_items(review_id):
    return q("""SELECT i.*, m.status AS media_status, m.progress AS media_progress, m.error AS media_error,
                       m.fps, m.frame_count, m.width, m.height, m.has_audio, m.built_at
                FROM items i LEFT JOIN media m ON m.version_id = i.version_id
                WHERE i.review_id=? ORDER BY i.position, i.id""", (review_id,))


def item(item_id):
    return q1("""SELECT i.*, m.status AS media_status, m.fps, m.frame_count, m.width, m.height
                 FROM items i LEFT JOIN media m ON m.version_id = i.version_id WHERE i.id=?""", (item_id,))


def item_for_version(review_id, version_id):
    return q1('SELECT id FROM items WHERE review_id=? AND version_id=?', (review_id, version_id))


def upsert_item(review_id, v, position):
    """v: version dict normalized by sg_bridge.version_info"""
    now = time.time()
    existing = item_for_version(review_id, v['version_id'])
    fields = ('version_code', 'entity_type', 'entity_id', 'entity_name', 'task_id', 'task_name',
              'artist_id', 'artist_name', 'sg_status', 'first_frame')
    if existing:
        ex(f"UPDATE items SET {', '.join(f + '=?' for f in fields)} WHERE id=?",
           tuple(v[f] for f in fields) + (existing['id'],))
        return existing['id'], False
    iid = ex(f"INSERT INTO items (review_id, position, version_id, added_at, {', '.join(fields)}) "
             f"VALUES (?,?,?,?,{','.join('?' * len(fields))})",
             (review_id, position, v['version_id'], now) + tuple(v[f] for f in fields))
    ensure_media_row(v['version_id'])
    return iid, True


# ─── media ────────────────────────────────────────────────────────────────────

def media(version_id):
    return q1('SELECT * FROM media WHERE version_id=?', (version_id,))


def media_many(version_ids):
    ids = [int(v) for v in version_ids]
    if not ids:
        return {}
    rows = q(f"SELECT * FROM media WHERE version_id IN ({','.join('?' * len(ids))})", ids)
    return {r['version_id']: r for r in rows}


def ensure_media_row(version_id):
    now = time.time()
    ex('INSERT OR IGNORE INTO media (version_id, status, updated_at, last_used) VALUES (?, ?, ?, ?)',
       (version_id, 'queued', now, now))


def touch_media(version_id):
    """Remember that this proxy is in use. At most hourly, so streaming a movie doesn't write on every request."""
    now = time.time()
    ex('UPDATE media SET last_used=? WHERE version_id=? AND last_used<?', (now, version_id, now - 3600))


def set_media(version_id, **kw):
    kw['updated_at'] = time.time()
    ex(f"UPDATE media SET {', '.join(k + '=?' for k in kw)} WHERE version_id=?",
       tuple(kw.values()) + (version_id,))


# ─── notes ────────────────────────────────────────────────────────────────────

def note_row(n):
    if n is None:
        return None
    n = dict(n)
    n['shapes'] = json.loads(n['shapes']) if n.get('shapes') else []
    n['files'] = note_files(n['id'])
    # Sent to ShotGrid, but its frame or a file didn't make it: the next publish finishes the job
    n['sg_unsent'] = bool((n.get('sg_note_id') or n.get('sg_reply_id')) and (
        (n['shapes'] and not n.get('parent_id') and not n.get('sg_frame_at')) or any(not f['sg_attachment_id'] for f in n['files'])))
    return n


def note_files(note_id):
    return q('SELECT id, name, size, sg_attachment_id FROM note_files WHERE note_id=? ORDER BY id', (note_id,))


def file_row(file_id):
    return q1('SELECT * FROM note_files WHERE id=?', (file_id,))


def note(note_id):
    return note_row(q1('SELECT * FROM notes WHERE id=?', (note_id,)))


def note_by_client_id(client_id):
    return note_row(q1('SELECT * FROM notes WHERE client_id=?', (client_id,)))


def review_notes(review_id):
    rows = q("""SELECT n.* FROM notes n JOIN items i ON i.id=n.item_id
                WHERE i.review_id=? AND n.deleted=0 ORDER BY n.frame, n.created_at""", (review_id,))
    return [note_row(r) for r in rows]


def item_notes(item_id, include_deleted=False):
    sql = 'SELECT * FROM notes WHERE item_id=?' + ('' if include_deleted else ' AND deleted=0')
    return [note_row(r) for r in q(sql + ' ORDER BY frame, created_at', (item_id,))]


# ─── drafts ───────────────────────────────────────────────────────────────────

def get_drafts(review_id, user_key):
    r = q1('SELECT data, updated_at FROM drafts WHERE review_id=? AND user_key=?', (review_id, user_key))
    return {'data': json.loads(r['data']), 'updated_at': r['updated_at']} if r else {'data': None, 'updated_at': 0}


def put_drafts(review_id, user_key, data, updated_at):
    ex("""INSERT INTO drafts (review_id, user_key, data, updated_at) VALUES (?,?,?,?)
          ON CONFLICT(review_id, user_key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at
          WHERE excluded.updated_at >= drafts.updated_at""",
       (review_id, user_key, json.dumps(data), updated_at))
