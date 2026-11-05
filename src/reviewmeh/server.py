#!/usr/bin/env python3
"""
ReviewMeh: review ShotGrid Versions with a frame-accurate player, drawings, threaded notes and a live room.

  ShotGrid button (AMI) → POST /ami/review       → opens the review, creating it if needed
  ShotGrid webhook      → POST /hooks/shotgrid   → live status changes, re-ingest of replaced movies
  Browser               → /r/{token}             → player, drawings, notes
  WebSocket             → /ws/{token}            → live notes, presence, presenter/follow, pointer
  Publish               → ShotGrid thread per version (Note, then Replies), with frames and files

{token} is either the review's own token (studio link: ShotGrid sign-in required, rights from the permission group)
or a share link key (rights from the permission group for people signed in; guests only if the link allows them).

Media is keyed by version id: any version of the review's project is ingested on demand and streamed
from /api/r/{token}/media/{version_id}/.

Run: reviewmeh serve   (single worker: websocket rooms and jobs live in memory)
"""
import asyncio
import hashlib
import hmac
import html
import importlib.util
import json
import logging
import os
import secrets
import shutil
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Header
from fastapi.responses import HTMLResponse, FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from itsdangerous import URLSafeTimedSerializer, BadSignature
from pydantic import BaseModel

from . import db
from . import exports
from . import media
from . import render
from . import sg_bridge
from . import config, hooks, system
from .config import (SECRET, PUBLIC_URL, LOGO_PATH, FAVICON_PATH, MEDIA_ROOT, CACHE_DAYS, APP_NAME, ADMIN_GROUPS, GROUP_RIGHTS, GUEST_FIELDS, SHARE_MAX_DAYS, MAX_UPLOAD_MB,
                     MAX_GUEST_UPLOAD_MB, PUBLISH_VERSIONS, AMI_KEY, WEBHOOK_SECRET, TRUST_AMI_USER, SCHEMA)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger = logging.getLogger('reviewmeh')

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, 'static')
COOKIE = 'rt_id'
COOKIE_AGE = 30 * 86400
BACKUP_EVERY = 86400
CACHE_EVERY = 86400
signer = URLSafeTimedSerializer(SECRET, salt='reviewmeh-identity')
LOOP: Optional[asyncio.AbstractEventLoop] = None


async def in_thread(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ─── websocket hub ────────────────────────────────────────────────────────────

class Hub:
    def __init__(self):
        self.rooms = {}  # review id -> {cid: {'ws', 'user', 'item_id', 'presenting', 'access'}}

    def presence(self, token):
        return [{'cid': cid, 'name': c['user']['name'], 'id': c['user'].get('id'),
                 'item_id': c['item_id'], 'presenting': c['presenting']}
                for cid, c in self.rooms.get(token, {}).items()]

    async def send(self, token, msg, exclude=None):
        data = json.dumps(msg)
        for cid, c in list(self.rooms.get(token, {}).items()):
            if cid == exclude:
                continue
            try:
                await c['ws'].send_text(data)
            except Exception:
                self.rooms.get(token, {}).pop(cid, None)

    async def send_version(self, version_id, msg, share_msg=None):
        """News about a version: every studio client, and share clients allowed to reach that version."""
        data, share_data = json.dumps(msg), json.dumps(share_msg or msg)
        for rid, room in list(self.rooms.items()):
            for cid, c in list(room.items()):
                a = c['access']
                if a.share and not version_known(a, version_id):
                    continue
                try:
                    await c['ws'].send_text(share_data if a.share else data)
                except Exception:
                    room.pop(cid, None)

    def send_version_threadsafe(self, version_id, msg, share_msg=None):
        if LOOP:
            asyncio.run_coroutine_threadsafe(self.send_version(version_id, msg, share_msg), LOOP)


hub = Hub()


def media_notifier(version_id):
    # Any open review may be streaming this version (item, cut neighbour, compare) → tell every room that can reach it
    row = db.media(version_id)
    hub.send_version_threadsafe(version_id, {'type': 'media', 'version_id': version_id, 'media': row},
                                {'type': 'media', 'version_id': version_id, 'media': share_media(row)})


async def expire_loop():
    """Share links stop working at their expiry even for pages left open."""
    while True:
        await asyncio.sleep(30)
        await in_thread(exports.prune)
        now = time.time()
        for room in list(hub.rooms.values()):
            for c in list(room.values()):
                if c['access'].share and db.share_by_key(c['access'].token)['expires_at'] <= now:
                    try:
                        await c['ws'].close(code=4410)
                    except Exception:
                        pass


async def backup_loop():
    await asyncio.sleep(60)
    while True:
        try:
            logger.info('DB backup → %s', await in_thread(db.backup))
        except Exception:
            logger.exception('DB backup failed')
        await asyncio.sleep(BACKUP_EVERY)


def clear_cache(in_use):
    """Delete copies of ShotGrid's files that haven't been needed for CACHE_DAYS, which can all be fetched again.
    Notes, their drawings, drafts and the files people attached aren't part of this."""
    if CACHE_DAYS <= 0:
        return
    cutoff = time.time() - CACHE_DAYS * 86400
    proxies, files = media.clear_idle(cutoff, in_use), 0
    for folder in (ATTACH_DIR, AVATAR_DIR):
        if os.path.isdir(folder):
            for f in os.scandir(folder):
                if f.is_file() and f.stat().st_mtime < cutoff:
                    os.remove(f.path)
                    files += 1
    if proxies or files:
        logger.info('Cache cleanup: removed %s proxies and %s other files unused for %s days', proxies, files, CACHE_DAYS)


async def cache_loop():
    while True:
        try:
            in_use = {i['version_id'] for rid, room in list(hub.rooms.items()) if room for i in db.review_items(rid)}  # someone has it open
            await in_thread(clear_cache, in_use)
        except Exception:
            logger.exception('Cache cleanup failed')
        await asyncio.sleep(CACHE_EVERY)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global LOOP
    LOOP = asyncio.get_running_loop()
    db.init()
    await in_thread(exports.clear_leftovers)
    await in_thread(clear_stale_uploads)
    media.set_notifier(media_notifier)
    await in_thread(media.resume_pending)
    _users.update(db.get_setting('user_groups', {}))
    _group_rights.update(GROUP_RIGHTS | db.get_setting('group_rights', {}))
    try:
        await asyncio.wait_for(refresh_users(), 20)
    except Exception:
        logger.warning('Could not read ShotGrid users at start; using the last known list', exc_info=True)
    if _users and not any(u['active'] and u['group'] in ADMIN_GROUPS for u in _users.values()):
        logger.warning('Nobody active is in the admin group %s, so nobody can open the Access tab. Set REVIEWMEH_ADMIN_GROUP to one of: %s',
                       sorted(ADMIN_GROUPS), sorted({u['group'] for u in _users.values() if u['active'] and u['group']}))
    tasks = [asyncio.create_task(backup_loop()), asyncio.create_task(expire_loop()), asyncio.create_task(users_loop()), asyncio.create_task(cache_loop())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(lifespan=lifespan, title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
app.mount('/static', StaticFiles(directory=STATIC_DIR), name='static')


# ─── identity ─────────────────────────────────────────────────────────────────
# Verified = ShotGrid vouched for the user: a signed AMI link, or signing in with ShotGrid (App Session
# Launcher). Their rights come from their ShotGrid permission group, and a deactivated account stops
# working within minutes. Their notes are created via sudo_as_login. Everyone else is a guest with a typed
# name (always suffixed "(guest)"), so nobody can pass as a ShotGrid user; their notes go out as the script user.

def me(request_or_ws):
    raw = request_or_ws.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        u = signer.loads(raw, max_age=COOKIE_AGE)
    except BadSignature:
        return None
    if u.get('id') and not u.get('v'):  # only ShotGrid can vouch for an id
        return None
    if u.get('v') and not (_users.get(str(u.get('id'))) or {}).get('active'):
        return None
    return u


def require_open(request):
    u = me(request)
    if not u or not u.get('v'):
        raise HTTPException(403, 'Sign in with ShotGrid to do this')
    if 'open' not in user_rights(u):
        raise HTTPException(403, NO_RIGHTS)
    return u


# ─── access: studio link or share link ────────────────────────────────────────
# People signed in with ShotGrid get the rights of their permission group, wherever they come from. Groups without
# 'open' only reach reviews through share links, and only that review. Guests get what the link's Share menu allows,
# if the link takes guests at all (off by default), and its password if it has one. Links always expire.

RIGHTS = [('view', 'Watch'), ('open', 'All reviews'), ('comment', 'Comment'), ('download', 'Download'),
          ('versions', 'Other versions'), ('status', 'Status'), ('publish', 'Publish'), ('add', 'Add versions'),
          ('present', 'Present'), ('share', 'Share')]
ALL_RIGHTS = {k for k, _ in RIGHTS}
GUEST_PERMS = ('comment', 'attach', 'download', 'versions', 'details')
MAX_SHARE_DAYS = SHARE_MAX_DAYS
NEEDS_USER = {'comment', 'attach', 'status', 'publish', 'add', 'share', 'present'}
SIGN_IN = 'Sign in with ShotGrid to open this review'
NO_RIGHTS = "Your ShotGrid group can't open this review"
_users = {}          # ShotGrid user id (str) -> {'group', 'active'}
_group_rights = {}   # group -> rights; loaded at start, changed from the dashboard


def user_group(user):
    return (_users.get(str((user or {}).get('id'))) or {}).get('group')


def user_rights(user):
    group = user_group(user)
    rights = set(ALL_RIGHTS) if group in ADMIN_GROUPS else set(_group_rights.get(group, ()))
    return rights | {'attach'} if 'comment' in rights else rights  # guests get it per link


def set_users(users):
    """Remember who is who; anyone whose group or active state changed reconnects, picking up their new rights."""
    changed = {k for k in set(users) | set(_users) if users.get(k) != _users.get(k)}
    if not changed:
        return set()
    _users.clear()
    _users.update(users)
    db.put_setting('user_groups', users)
    return changed


async def kick(who):
    """Close the sockets of signed-in users `who(user)` picks: their pages reconnect and reload their rights."""
    for room in list(hub.rooms.values()):
        for c in list(room.values()):
            if c['user'].get('v') and who(c['user']):
                try:
                    await c['ws'].close(code=4012)
                except Exception:
                    pass


async def refresh_users():
    changed = set_users(await in_thread(sg_bridge.user_groups))
    if changed:
        await kick(lambda u: str(u.get('id')) in changed)


async def users_loop():
    while True:
        await asyncio.sleep(300)
        try:
            await refresh_users()
        except Exception:
            logger.warning('Could not refresh ShotGrid users', exc_info=True)


class Access:
    def __init__(self, review, user, perms, token, share=None):
        self.review, self.user, self.perms, self.token, self.share = review, user, perms, token, share

    @property
    def room(self):
        return self.review['id']

    def need(self, perm):
        if perm in NEEDS_USER and not self.user:
            raise HTTPException(401, 'Identify first')
        if perm not in self.perms:
            raise HTTPException(403, 'Not allowed with this link')
        return self.user


def live_share(token):
    share = db.share_by_key(token)
    if not share or share['revoked_at']:
        raise HTTPException(404, 'Review not found')
    if share['expires_at'] <= time.time():
        raise HTTPException(410, 'This link has expired')
    return share


def access(token, conn):
    user = me(conn)
    verified = bool(user and user.get('v'))
    rights = user_rights(user) if verified else set()
    review = db.review_by_token(token)
    if review:
        if not verified:
            raise HTTPException(401, SIGN_IN)
        if 'open' not in rights:
            raise HTTPException(403, NO_RIGHTS)
        return Access(review, user, rights | {'view', 'details'}, token)
    share = live_share(token)
    review = db.review(share['review_id'])
    if verified:
        if 'open' in rights:  # same as their studio link
            return Access(review, user, rights | {'view', 'details'}, token)
        if 'view' not in rights:
            raise HTTPException(403, NO_RIGHTS)
        return Access(review, user, rights | {'details'}, token, share)
    if not share['guests']:
        raise HTTPException(401, SIGN_IN)
    if share['password_hash'] and not password_ok(conn, share):
        raise HTTPException(401, 'Password required')
    return Access(review, user, {'view'} | {k for k in GUEST_PERMS if share['perms'].get(k)}, token, share)


# ─── guest passwords ──────────────────────────────────────────────────────────

_pw_fails = {}  # (share id, ip or None for the whole link) -> times of wrong passwords


def hash_password(password):
    salt = secrets.token_bytes(16)
    return f"{salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()}"


def check_password(password, stored):
    salt, digest = stored.split('$')
    got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(got.hex(), digest)


def pw_cookie(share):
    return f"rt_pw_{share['id']}"


def pw_proof(share):
    """Tied to the stored hash, without exposing it."""
    return hashlib.sha256(share['password_hash'].encode()).hexdigest()[:24]


def password_ok(conn, share):
    raw = conn.cookies.get(pw_cookie(share))
    try:
        return bool(raw) and signer.loads(raw, max_age=COOKIE_AGE).get('pw') == pw_proof(share)
    except (BadSignature, AttributeError):
        return False


def pw_throttled(share_id, ip):
    """5 wrong tries per address in 5 minutes, 50 per link in an hour."""
    now = time.time()
    for key, window, limit in (((share_id, ip), 300, 5), ((share_id, None), 3600, 50)):
        tries = _pw_fails[key] = [t for t in _pw_fails.get(key, []) if now - t < window]
        if len(tries) >= limit:
            return True
    return False


_in_project = set()  # (project id, version id) already confirmed with ShotGrid


def version_known(a, version_id):
    """Without asking ShotGrid: may this share link reach the version?"""
    return bool(db.item_for_version(a.room, version_id)) or \
        ('versions' in a.perms and (a.review['project_id'], version_id) in _in_project)


async def reachable(a, version_ids):
    """The versions this access may reach: a studio link all of them, a share link what its rights allow."""
    if not a.share:
        return list(version_ids)
    unknown = [v for v in version_ids if not version_known(a, v)]
    if unknown and 'versions' in a.perms:
        for v in await in_thread(project_versions, a.review, unknown):
            _in_project.add((a.review['project_id'], v['version_id']))
    return [v for v in version_ids if version_known(a, v)]


def share_media(row):
    return {k: v for k, v in row.items() if k not in ('src_name', 'error')} if row else row


def note_out(n):
    """Notes as sent to browsers: authors' logins stay on the server."""
    return {k: v for k, v in n.items() if k != 'author_login'} if n else n


def items_out(a, items):
    return [{**i, 'media_error': None} for i in items] if a is None or a.share else items


def user_key(u):
    return f"u{u['id']}" if u.get('id') else f"g:{u['name']}"


def set_identity(resp, user):
    resp.set_cookie(COOKIE, signer.dumps(user), max_age=COOKIE_AGE, httponly=True, samesite='lax',
                    secure=PUBLIC_URL.startswith('https'))


def auth_param(user):
    return signer.dumps({'auth': user})


def get_item(review, item_id):
    it = db.item(item_id)
    if not it or it['review_id'] != review['id']:
        raise HTTPException(404, 'Item not found')
    return it


def project_versions(review, version_ids):
    """SG versions that belong to the review's project (guards on-demand ingest and auto-added items)."""
    vs = sg_bridge.fetch_versions(version_ids)
    return [v for v in vs if (v.get('project') or {}).get('id') == review['project_id']]


# ─── pages ────────────────────────────────────────────────────────────────────

def page(name):
    with open(os.path.join(STATIC_DIR, name)) as f:
        return f.read().replace('{{APP_NAME}}', html.escape(APP_NAME))


@app.get('/config.js')
async def config_js():
    settings = config.public() | {'pdf': importlib.util.find_spec('weasyprint') is not None}
    return Response(f'window.APP = {json.dumps(settings)};\n'
                    "document.documentElement.style.setProperty('--accent', APP.accent);\n"
                    "document.documentElement.style.setProperty('--accent2', APP.accent_hover);\n",
                    media_type='text/javascript', headers={'Cache-Control': 'no-cache'})


@app.get('/', response_class=HTMLResponse)
async def dashboard():
    return page('index.html')


@app.get('/r/{token}', response_class=HTMLResponse)
async def review_page(request: Request, token: str, auth: Optional[str] = None):
    if not db.review_by_token(token) and not db.share_by_key(token):
        raise HTTPException(404, 'Review not found')
    if auth and TRUST_AMI_USER and db.review_by_token(token):
        try:
            user = signer.loads(auth, max_age=7 * 86400)['auth']
        except (BadSignature, KeyError, TypeError):
            raise HTTPException(403, 'Invalid or expired link — open the review again from ShotGrid')
        qs = '&'.join(f'{k}={v}' for k, v in request.query_params.items() if k != 'auth')
        resp = RedirectResponse(f'/r/{token}' + (f'?{qs}' if qs else ''), status_code=303)
        set_identity(resp, user)
        return resp
    return page('review.html')


@app.get('/logo.svg')
async def logo():
    return FileResponse(LOGO_PATH, media_type='image/svg+xml')


@app.get('/favicon.svg')
async def favicon():
    return FileResponse(FAVICON_PATH, media_type='image/svg+xml')


@app.get('/health')
async def health():
    return {'ok': True}


# ─── identity API ─────────────────────────────────────────────────────────────

@app.get('/api/me')
async def api_me(request: Request):
    u = me(request)
    verified = bool(u and u.get('v'))
    return {'user': u, 'rights': sorted(user_rights(u)) if verified else [], 'admin': verified and user_group(u) in ADMIN_GROUPS}


# What the sign-in page offers on this address
@app.get('/api/gate/{token}')
async def gate(token: str):
    if db.review_by_token(token):
        return {'guests': False, 'password': False}
    share = live_share(token)
    return {'guests': bool(share['guests']), 'password': bool(share['guests'] and share['password_hash'])}


class IdentifyReq(BaseModel):
    token: str
    guest_name: str
    password: Optional[str] = None


@app.post('/api/identify')
async def identify(req: IdentifyReq, request: Request):
    if db.review_by_token(req.token):
        raise HTTPException(403, SIGN_IN)
    share = live_share(req.token)
    if not share['guests']:
        raise HTTPException(403, SIGN_IN)
    name = ' '.join(req.guest_name.replace('(guest)', '').split())[:60]
    if not name:
        raise HTTPException(400, 'Name required')
    if share['password_hash'] and not password_ok(request, share):
        ip = request.client.host if request.client else ''
        if pw_throttled(share['id'], ip):
            raise HTTPException(429, 'Too many tries. Wait a few minutes')
        if not await in_thread(check_password, (req.password or '')[:200], share['password_hash']):
            now = time.time()
            for key in ((share['id'], ip), (share['id'], None)):
                _pw_fails.setdefault(key, []).append(now)
            logger.warning('Wrong password on share link %s from %s', share['id'], ip)
            raise HTTPException(403, 'Wrong password')
    user = {'id': None, 'name': f'{name} (guest)', 'login': None, 'v': 0}
    resp = Response(json.dumps({'user': user}), media_type='application/json')
    set_identity(resp, user)
    if share['password_hash']:
        resp.set_cookie(pw_cookie(share), signer.dumps({'pw': pw_proof(share)}), max_age=int(share['expires_at'] - time.time()),
                        httponly=True, samesite='lax', secure=PUBLIC_URL.startswith('https'))
    return resp


_logins = {}  # our login id → (ShotGrid session request id, started at)


@app.post('/api/login')
async def login_start():
    """Start signing in with ShotGrid: returns the approval page to open and an id to poll."""
    now = time.time()
    for k in [k for k, (_, t) in _logins.items() if now - t > 600]:
        del _logins[k]
    try:
        r = await in_thread(sg_bridge.app_session_start)
    except RuntimeError as e:
        raise HTTPException(503, str(e))
    lid = secrets.token_urlsafe(db.TOKEN_BYTES)
    _logins[lid] = (r['sessionRequestId'], now)
    return {'id': lid, 'url': r['url']}


@app.get('/api/login/{lid}')
async def login_poll(lid: str):
    """{user: null} until approved in ShotGrid; then signs the browser in as that verified user."""
    if lid not in _logins:
        raise HTTPException(410, 'Sign-in expired')
    try:
        st = await in_thread(sg_bridge.app_session_poll, _logins[lid][0])
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    if st is None:
        _logins.pop(lid, None)
        raise HTTPException(410, 'Sign-in was declined or expired')
    if not st['approved']:
        return {'user': None}
    _logins.pop(lid, None)
    user = {**st['user'], 'v': 1}
    if str(user['id']) not in _users:  # a brand new account
        await refresh_users()
    resp = Response(json.dumps({'user': user}), media_type='application/json')
    set_identity(resp, user)
    return resp


@app.post('/api/logout')
async def logout():
    resp = Response('{}', media_type='application/json')
    resp.delete_cookie(COOKIE)
    return resp


# ─── review API ───────────────────────────────────────────────────────────────

def share_out(s):
    return {k: s[k] for k in ('id', 'key', 'perms', 'created_by_name', 'created_at', 'expires_at')} | \
        {'url': f"{PUBLIC_URL}/r/{s['key']}", 'guests': bool(s['guests']), 'has_password': bool(s['password_hash'])}


def fill_playlist_name(review):
    if review['playlist_id'] and not review.get('playlist_name'):
        pl = sg_bridge.sg().find_one('Playlist', [['id', 'is', review['playlist_id']]], ['code'])
        if pl:
            db.ex('UPDATE reviews SET playlist_name=? WHERE id=?', (pl['code'], review['id']))
            review['playlist_name'] = pl['code']
    return review


@app.get('/api/r/{token}')
async def review_data(token: str, request: Request):
    a = access(token, request)
    r = a.review
    try:
        r = await in_thread(fill_playlist_name, r)
    except Exception:
        logger.warning('Could not read playlist name for review %s', token)
    cut_level = None
    if 'versions' in a.perms:
        try:
            cut_level = await in_thread(sg_bridge.project_cut, r['project_id'])
        except Exception:
            logger.warning('Could not read how project %s groups its shots', r['project_id'])
    await in_thread(media.ensure, [i['version_id'] for i in db.review_items(r['id'])])  # rebuilds proxies the cleanup removed
    return {'review': {k: r.get(k) for k in ('id', 'name', 'project_id', 'project_name', 'playlist_id',
                                             'playlist_name', 'created_by_name', 'created_at')} | {'token': token},
            'access': {'perms': sorted(a.perms), 'share': bool(a.share),
                       'expires_at': a.share['expires_at'] if a.share else None},
            'studio_url': f"{PUBLIC_URL}/r/{r['token']}" if 'open' in a.perms else None,
            'cut_label': cut_level['label'] if cut_level else None,
            'sort_fields': {'task_order': f'sg_task.Task.{SCHEMA.task_order}', 'cut_order': f'entity.Shot.{SCHEMA.cut_order}'},
            'items': items_out(a, db.review_items(r['id'])),
            'notes': [note_out(n) for n in db.review_notes(r['id'])],
            'me': a.user,
            'presence': hub.presence(a.room)}


@app.get('/api/r/{token}/media/{version_id}/{kind}')
async def media_file(token: str, version_id: int, kind: str, request: Request, dl: int = 0):
    a = access(token, request)
    if dl:
        a.need('download')
    if not await reachable(a, [version_id]):
        raise HTTPException(404, 'Media not found')
    if kind == 'proxy.mp4':
        path, mt = media.proxy_path(version_id), 'video/mp4'
    elif kind == 'thumb.jpg':
        path, mt = media.thumb_path(version_id), 'image/jpeg'
    else:
        raise HTTPException(404)
    if not db.media(version_id) or not os.path.isfile(path):
        raise HTTPException(404, 'Media not ready')
    if kind == 'proxy.mp4':
        db.touch_media(version_id)
    if dl:
        it = db.q1('SELECT version_code FROM items WHERE version_id=?', (version_id,))
        name = f"{(it or {}).get('version_code') or version_id}{os.path.splitext(kind)[1]}"
        return FileResponse(path, media_type=mt, filename=name)
    return FileResponse(path, media_type=mt, headers={'Cache-Control': 'private, max-age=3600'})


class VersionIdsReq(BaseModel):
    version_ids: list[int]


@app.post('/api/r/{token}/media')
async def ensure_media(token: str, req: VersionIdsReq, request: Request):
    """Start ingest for versions streamed outside the item list (cut neighbours, compare). Returns media rows."""
    a = access(token, request)
    review = a.review
    ids = await reachable(a, list(dict.fromkeys(req.version_ids))[:400])
    known = db.media_many(ids)
    unknown = [v for v in ids if v not in known]
    allowed = set(known) | {v['version_id'] for v in await in_thread(project_versions, review, unknown)} if unknown else set(known)
    rows = await in_thread(media.ensure, [v for v in ids if v in allowed])
    return {str(k): share_media(v) if a.share else v for k, v in rows.items()}


@app.get('/api/r/{token}/frame/{item_id}/{frame}.png')
async def frame_png(token: str, item_id: int, frame: int, request: Request, note: Optional[int] = None):
    a = access(token, request)
    a.need('download')
    it = get_item(a.review, item_id)
    if it['media_status'] != 'ready':
        raise HTTPException(409, 'Media not ready')
    n = db.note(note) if note else None
    if n and n['item_id'] != item_id:
        raise HTTPException(404)
    n = n or {'frame': frame, 'shapes': [], 'author_name': ''}
    path = await in_thread(render.render_note_frame, it, n, bool(note))
    data = open(path, 'rb').read()
    os.remove(path)
    name = f"{it['version_code']}.{it['first_frame'] + n['frame']}.png"
    return Response(data, media_type='image/png', headers={'Content-Disposition': f'inline; filename="{name}"',
                                                           'Cache-Control': 'private, max-age=3600'})


def add_versions(review, versions):
    pos = (db.q1('SELECT MAX(position) AS p FROM items WHERE review_id=?', (review['id'],))['p'] or 0)
    added = []
    for v in versions:
        pos += 1
        iid, new = db.upsert_item(review['id'], v, pos)
        media.enqueue(v['version_id'])
        if new:
            added.append(iid)
    return added


@app.post('/api/r/{token}/items')
async def add_items(token: str, req: VersionIdsReq, request: Request):
    a = access(token, request)
    a.need('add')
    versions = await in_thread(project_versions, a.review, req.version_ids)
    added = await in_thread(add_versions, a.review, versions)
    await hub.send(a.room, {'type': 'items'})
    return {'added': added}


@app.delete('/api/r/{token}/items/{item_id}')
async def remove_item(token: str, item_id: int, request: Request):
    a = access(token, request)
    a.need('add')
    it = get_item(a.review, item_id)
    if db.q1('SELECT 1 FROM notes WHERE item_id=? AND deleted=0', (it['id'],)):
        raise HTTPException(409, 'Item has notes — delete them first')
    db.ex('DELETE FROM notes WHERE item_id=?', (it['id'],))
    db.ex('DELETE FROM items WHERE id=?', (it['id'],))
    await hub.send(a.room, {'type': 'items'})
    return {'ok': True}


@app.post('/api/r/{token}/items/{item_id}/reingest')
async def reingest(token: str, item_id: int, request: Request):
    a = access(token, request)
    a.need('add')
    it = get_item(a.review, item_id)
    await in_thread(media.enqueue, it['version_id'], True)
    return {'ok': True}


@app.get('/api/r/{token}/search')
async def search(token: str, request: Request, q: str = '', episode_id: Optional[int] = None,
                 task: Optional[str] = None, status: Optional[str] = None):
    a = access(token, request)
    a.need('add')
    review = a.review
    q = q.strip()
    if len(q) < 2 and not (episode_id or task or status):
        return []
    return await in_thread(lambda: sg_bridge.search_versions(review['project_id'], q, episode_id, task, status))


@app.get('/api/r/{token}/filters')
async def filters(token: str, request: Request):
    a = access(token, request)
    a.need('add')
    return await in_thread(sg_bridge.project_filters, a.review['project_id'])


@app.get('/api/r/{token}/versions')
async def versions_of(token: str, entity_type: str, entity_id: int, request: Request):
    """Every version of a shot/asset with a movie (compare picker), with media state."""
    a = access(token, request)
    a.need('versions')
    vs = await in_thread(sg_bridge.entity_versions, entity_type, entity_id)
    if a.share:
        vs = [v for v in vs if (v.get('project') or {}).get('id') == a.review['project_id']]
        _in_project.update((a.review['project_id'], v['version_id']) for v in vs)
    m = db.media_many([v['version_id'] for v in vs])
    for v in vs:
        v.pop('project', None)
        v['media'] = share_media(m.get(v['version_id'])) if a.share else m.get(v['version_id'])
    return vs


@app.get('/api/r/{token}/cut')
async def cut(token: str, shot_id: int, request: Request, fresh: int = 0):
    """The shot's cut, in order with every version per shot (mini-cut, full cut, compositor)."""
    a = access(token, request)
    a.need('versions')
    if a.share and await in_thread(sg_bridge.shot_project_id, shot_id) != a.review['project_id']:
        raise HTTPException(404, 'Shot is not in this project')
    found = await in_thread(sg_bridge.shot_cut_parent, shot_id)
    if not found:
        raise HTTPException(404, "This shot isn't in a cut")
    data = await in_thread(sg_bridge.load_cut, found[0], found[1]['id'], bool(fresh))
    ids = [v['version_id'] for s in data['shots'] for v in s['versions']]
    if a.share:
        _in_project.update((a.review['project_id'], v) for v in ids)
    m = db.media_many(ids)
    return {**data, 'media': {str(k): share_media(v) if a.share else v for k, v in m.items()}}


@app.get('/api/r/{token}/statuses')
async def statuses(token: str, request: Request):
    return await in_thread(sg_bridge.version_statuses, access(token, request).review['project_id'])


class StatusReq(BaseModel):
    status: str


@app.post('/api/r/{token}/versions/{version_id}/status')
async def set_status(token: str, version_id: int, req: StatusReq, request: Request):
    a = access(token, request)
    user, review = a.need('status'), a.review
    if not user.get('v'):
        raise HTTPException(403, 'Sign in with ShotGrid to do this')
    if req.status not in {s['code'] for s in await in_thread(sg_bridge.version_statuses, review['project_id'])}:
        raise HTTPException(400, 'Unknown status')
    if a.share and not await reachable(a, [version_id]):
        raise HTTPException(404, 'Version not in this review')
    if not db.item_for_version(review['id'], version_id) and not await in_thread(project_versions, review, [version_id]):
        raise HTTPException(404, 'Version not in this project')
    await in_thread(sg_bridge.set_version_status, version_id, req.status, user.get('login'))
    db.ex('UPDATE items SET sg_status=? WHERE version_id=?', (req.status, version_id))
    await hub.send_version(version_id, {'type': 'status', 'version_id': version_id, 'status': req.status, 'by': user['name']})
    return {'ok': True}


# ─── details and shot context (right panel) ───────────────────────────────────

@app.get('/api/r/{token}/fields')
async def fields(token: str, request: Request, path: str = ''):
    """Fields on offer in Details. `path` (entity.Shot) opens a linked entity: studio users only."""
    a = access(token, request)
    a.need('details')
    full = 'open' in a.perms
    if path:
        if not full:
            raise HTTPException(403, 'Not allowed with this link')
        try:
            return await in_thread(sg_bridge.linked_fields, path)
        except ValueError:
            raise HTTPException(404, 'Unknown field')
    rows = await in_thread(sg_bridge.version_fields)
    return rows if full else [{k: f[k] for k in ('key', 'name', 'type')} for f in rows if f['key'] in GUEST_FIELDS]


@app.get('/api/r/{token}/sort')
async def sort_values(token: str, request: Request, field: str):
    """One field of every version in the review, plain or linked, to sort the list by. Same limits as Details."""
    a = access(token, request)
    a.need('details')
    if 'open' not in a.perms and field not in GUEST_FIELDS:
        raise HTTPException(403, 'Not allowed with this link')
    defs = await in_thread(sg_bridge.field_defs, [field])
    if not defs:
        raise HTTPException(404, 'Unknown field')
    ids = [i['version_id'] for i in db.review_items(a.review['id'])]
    return {'def': defs[0], 'vals': await in_thread(sg_bridge.versions_field, ids, field)}


@app.get('/api/r/{token}/versions/{version_id}/details')
async def version_details(token: str, version_id: int, request: Request, fields: str = ''):
    a = access(token, request)
    a.need('details')
    if not await reachable(a, [version_id]):
        raise HTTPException(404, 'Version not found')
    keys = list(dict.fromkeys(fields.split(',')))[:60]
    if 'open' not in a.perms:
        keys = [k for k in keys if k in GUEST_FIELDS]
    defs = await in_thread(sg_bridge.field_defs, keys)
    if not defs:
        return {'vals': {}, 'defs': []}
    vals = await in_thread(sg_bridge.version_details, version_id, [d['key'] for d in defs])
    if vals is None:
        raise HTTPException(404, 'Version not found')
    return {'vals': vals, 'defs': defs}


@app.get('/api/r/{token}/entity_notes')
async def entity_notes(token: str, entity_type: str, entity_id: int, request: Request):
    """ShotGrid's own notes for the shot/asset. Internal history: never for guests."""
    a = access(token, request)
    if 'open' not in a.perms or 'versions' not in a.perms or entity_type not in ('Shot', 'Asset'):
        return []
    return await in_thread(sg_bridge.entity_notes, a.review['project_id'], entity_type, entity_id)


ATTACH_DIR = os.path.join(MEDIA_ROOT, 'attachments')  # on the NAS: full-size frames, until clear_cache removes them


def attachment_file(attachment_id, project_id):
    """Local copy of an annotated frame attached to a ShotGrid Note (attachments never change)."""
    path = os.path.join(ATTACH_DIR, f'{attachment_id}.img')
    if os.path.exists(path):
        return path
    url = sg_bridge.note_attachment_url(attachment_id, project_id)
    if not url or not url.startswith('https://'):
        return None
    with urllib.request.urlopen(url, timeout=30) as r:
        data = r.read(30_000_000)
    os.makedirs(ATTACH_DIR, exist_ok=True)
    tmp = f'{path}.{threading.get_ident()}'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    return path


@app.get('/api/r/{token}/attachment/{attachment_id}')
async def note_attachment(token: str, attachment_id: int, request: Request):
    a = access(token, request)
    if 'open' not in a.perms or 'versions' not in a.perms:
        raise HTTPException(403, 'Not allowed with this link')
    path = await in_thread(attachment_file, attachment_id, a.review['project_id'])
    if not path:
        raise HTTPException(404, 'Attachment not found')
    return FileResponse(path, media_type='image/png', headers={'Cache-Control': 'private, max-age=86400'})


AVATAR_DIR = os.path.join(MEDIA_ROOT, 'avatars')
AVATAR_MAX_AGE = 86400


def avatar_file(user_id):
    """Local copy of the user's ShotGrid picture (ShotGrid's own urls expire); an empty file = no picture."""
    path = os.path.join(AVATAR_DIR, f'{user_id}.img')
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < AVATAR_MAX_AGE:
        return path
    url = sg_bridge.user_image(user_id)
    data = b''
    if url and url.startswith('https://'):
        with urllib.request.urlopen(url, timeout=15) as r:
            data = r.read(5_000_000)
    os.makedirs(AVATAR_DIR, exist_ok=True)
    tmp = f'{path}.{threading.get_ident()}'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    return path


@app.get('/api/avatar/{user_id}')
async def avatar(user_id: int, request: Request, t: Optional[str] = None):
    """Studio users see anyone's, everyone their own; a share link only those who wrote notes in that review or are in the room."""
    user = me(request)
    if not (user and user.get('v') and ('open' in user_rights(user) or user.get('id') == user_id)):
        if not t:
            raise HTTPException(403, 'Not allowed')
        a = access(t, request)
        if not db.q1('SELECT 1 FROM notes n JOIN items i ON i.id=n.item_id WHERE i.review_id=? AND n.author_id=? LIMIT 1',
                     (a.room, user_id)) and not any(p['id'] == user_id for p in hub.presence(a.room)):
            raise HTTPException(403, 'Not allowed')
    no_pic = Response(status_code=404, headers={'Cache-Control': 'private, max-age=3600'})
    try:
        path = await in_thread(avatar_file, user_id)
    except Exception:
        logger.warning('Could not read the picture of user %s', user_id)
        return no_pic
    if not os.path.getsize(path):
        return no_pic
    return FileResponse(path, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=3600'})


# ─── drafts (unposted drawings + comment text) ────────────────────────────────

class DraftsReq(BaseModel):
    data: dict
    updated_at: float


@app.get('/api/r/{token}/drafts')
async def get_drafts(token: str, request: Request):
    a = access(token, request)
    return db.get_drafts(a.review['id'], user_key(a.need('comment')))


@app.put('/api/r/{token}/drafts')
async def put_drafts(token: str, req: DraftsReq, request: Request):
    a = access(token, request)
    user = a.need('comment')
    blob = json.dumps(req.data)
    if len(blob) > 5_000_000:
        raise HTTPException(400, 'Drafts too large')
    db.put_drafts(a.review['id'], user_key(user), req.data, req.updated_at)
    return {'ok': True}


# ─── notes ────────────────────────────────────────────────────────────────────

class NoteReq(BaseModel):
    item_id: Optional[int] = None
    version_id: Optional[int] = None   # comment on a version not yet in the review (cut neighbour) → auto-added
    parent_id: Optional[int] = None
    client_id: Optional[str] = None    # idempotency key: retries return the already-created note
    frame: Optional[int] = None
    hold: Optional[int] = None
    text: Optional[str] = None
    shapes: Optional[list] = None
    resolved: Optional[bool] = None
    file_ids: list[int] = []


def clean_shapes(shapes):
    if shapes is None:
        return None
    if len(shapes) > 500:
        raise HTTPException(400, 'Too many shapes')
    out = json.dumps(shapes)
    if len(out) > 2_000_000:
        raise HTTPException(400, 'Drawing too large')
    return out


def clean_hold(hold):
    return -1 if hold == -1 else max(0, min(int(hold or 0), 100000))


@app.post('/api/r/{token}/notes')
async def create_note(token: str, req: NoteReq, request: Request):
    a = access(token, request)
    user, review = a.need('comment'), a.review
    if req.client_id:
        req.client_id = req.client_id[:64]
        existing = db.note_by_client_id(req.client_id)
        if existing:
            get_item(review, existing['item_id'])
            return note_out(existing)
    if req.parent_id:
        parent = db.note(req.parent_id)
        if not parent or parent['deleted']:
            raise HTTPException(404, 'Parent note not found')
        it = get_item(review, parent['item_id'])
        frame, hold, shapes = parent['frame'], 0, None
    else:
        if not req.item_id and req.version_id:
            row = db.item_for_version(review['id'], req.version_id)
            if not row:
                a.need('versions')
                vs = await in_thread(project_versions, review, [req.version_id])
                if not vs:
                    raise HTTPException(404, 'Version not in this project')
                await in_thread(add_versions, review, vs)
                await hub.send(a.room, {'type': 'items'})
                row = db.item_for_version(review['id'], req.version_id)
            req.item_id = row['id']
        it = get_item(review, req.item_id)
        frame = max(0, int(req.frame or 0))
        hold = clean_hold(req.hold)
        shapes = clean_shapes(req.shapes or [])
    text = (req.text or '').strip()
    if req.file_ids:
        a.need('attach')
    if not text and not (shapes and shapes != '[]') and not req.file_ids:
        raise HTTPException(400, 'Empty note')
    now = time.time()
    nid = db.ex("""INSERT INTO notes (item_id, parent_id, author_id, author_name, author_login, author_verified,
                                      frame, hold, text, shapes, created_at, updated_at, client_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (it['id'], req.parent_id, user.get('id'), user['name'], user.get('login'), int(bool(user.get('v'))),
                 frame, hold, text, shapes, now, now, req.client_id))
    for fid in req.file_ids:  # only the poster's own, still unattached uploads in this review
        db.ex('UPDATE note_files SET note_id=? WHERE id=? AND review_id=? AND user_key=? AND note_id IS NULL',
              (nid, fid, review['id'], user_key(user)))
    n = note_out(db.note(nid))
    await hub.send(a.room, {'type': 'note', 'note': n})
    return n


def own_note(request, token, note_id):
    a = access(token, request)
    user = a.need('comment')
    n = db.note(note_id)
    if not n or n['deleted']:
        raise HTTPException(404, 'Note not found')
    get_item(a.review, n['item_id'])
    mine = (user.get('id') and n['author_id'] == user['id']) or (not user.get('id') and n['author_name'] == user['name'])
    return a, n, bool(mine)


@app.patch('/api/r/{token}/notes/{note_id}')
async def update_note(token: str, note_id: int, req: NoteReq, request: Request):
    a, n, mine = own_note(request, token, note_id)
    sets, args = [], []
    if req.resolved is not None:  # anyone may resolve / reopen
        sets.append('resolved=?'); args.append(int(req.resolved))
    if any(v is not None for v in (req.text, req.shapes, req.frame, req.hold)):
        if not mine:
            raise HTTPException(403, 'Only the author can edit this note')
        if req.text is not None:
            sets.append('text=?'); args.append(req.text.strip())
        if req.shapes is not None and n['parent_id'] is None:
            sets.append('shapes=?'); args.append(clean_shapes(req.shapes))
        if req.frame is not None and n['parent_id'] is None:
            sets.append('frame=?'); args.append(max(0, int(req.frame)))
        if req.hold is not None:
            sets.append('hold=?'); args.append(clean_hold(req.hold))
    if not sets:
        return note_out(n)
    sets.append('updated_at=?'); args.append(time.time())
    db.ex(f"UPDATE notes SET {', '.join(sets)} WHERE id=?", tuple(args) + (note_id,))
    n = note_out(db.note(note_id))
    await hub.send(a.room, {'type': 'note', 'note': n})
    return n


@app.delete('/api/r/{token}/notes/{note_id}')
async def delete_note(token: str, note_id: int, request: Request):
    a, n, mine = own_note(request, token, note_id)
    if not mine:
        raise HTTPException(403, 'Only the author can delete this note')
    now = time.time()
    db.ex('UPDATE notes SET deleted=1, updated_at=? WHERE id=? OR parent_id=?', (now, note_id, note_id))
    await hub.send(a.room, {'type': 'note_deleted', 'id': note_id, 'item_id': n['item_id']})
    return {'ok': True}


# ─── attachments ──────────────────────────────────────────────────────────────

UPLOAD_DIR = os.path.join(MEDIA_ROOT, 'uploads')
MAX_UPLOAD = MAX_UPLOAD_MB * 1024 * 1024
MAX_GUEST_UPLOAD = MAX_GUEST_UPLOAD_MB * 1024 * 1024
BLOCKED_EXT = {'exe', 'bat', 'cmd', 'com', 'msi', 'scr', 'ps1', 'vbs', 'dll', 'jar', 'sh', 'app', 'html', 'htm', 'svg', 'js'}
INLINE_TYPES = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'gif': 'image/gif', 'webp': 'image/webp'}
UNLINKED_MAX_AGE = 14 * 86400


def clean_filename(name):
    name = os.path.basename((name or '').replace('\\', '/')).strip()
    name = ''.join(c for c in name if c.isprintable() and c not in '<>:"|?*')[-150:].strip('. ')
    return name or 'file'


def clear_stale_uploads():
    """Uploads never posted (a composer abandoned for weeks). Their drafts forget them too."""
    for f in db.q('SELECT id, path FROM note_files WHERE note_id IS NULL AND created_at<?', (time.time() - UNLINKED_MAX_AGE,)):
        if f['path']:
            shutil.rmtree(os.path.dirname(f['path']), ignore_errors=True)
        db.ex('DELETE FROM note_files WHERE id=?', (f['id'],))


@app.post('/api/r/{token}/files')
async def upload_file(token: str, request: Request, name: str = ''):
    """The file is the raw request body; it joins a note when the note is posted."""
    a = access(token, request)
    user = a.need('attach')
    name = clean_filename(name)
    if name.rsplit('.', 1)[-1].lower() in BLOCKED_EXT:
        raise HTTPException(415, 'This kind of file can\'t be attached')
    limit = MAX_UPLOAD if user.get('v') else MAX_GUEST_UPLOAD
    too_big = HTTPException(413, f'Files can be up to {limit // (1024 * 1024)} MB')
    if int(request.headers.get('content-length') or 0) > limit:
        raise too_big
    fid = db.ex('INSERT INTO note_files (review_id, user_key, name, created_at) VALUES (?,?,?,?)',
                (a.room, user_key(user), name, time.time()))
    folder = os.path.join(UPLOAD_DIR, str(fid))
    path = os.path.join(folder, name)
    size = 0
    try:
        await in_thread(os.makedirs, folder, 0o755, True)
        f = await in_thread(open, path, 'wb')
        try:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise too_big
                await in_thread(f.write, chunk)
        finally:
            await in_thread(f.close)
        if not size:
            raise HTTPException(400, 'Empty file')
    except BaseException:
        await in_thread(shutil.rmtree, folder, True)
        db.ex('DELETE FROM note_files WHERE id=?', (fid,))
        raise
    db.ex('UPDATE note_files SET path=?, size=? WHERE id=?', (path, size, fid))
    return {'id': fid, 'name': name, 'size': size}


@app.delete('/api/r/{token}/files/{file_id}')
async def discard_file(token: str, file_id: int, request: Request):
    a = access(token, request)
    user = a.need('attach')
    f = db.file_row(file_id)
    if not f or f['review_id'] != a.room or f['note_id'] or f['user_key'] != user_key(user):
        raise HTTPException(404, 'File not found')
    await in_thread(shutil.rmtree, os.path.dirname(f['path']), True)
    db.ex('DELETE FROM note_files WHERE id=?', (file_id,))
    return {'ok': True}


@app.get('/api/r/{token}/files/{file_id}')
async def get_file(token: str, file_id: int, request: Request):
    """Pictures show in the page; anything else is a download. Nothing a guest uploaded can run in this site."""
    a = access(token, request)
    f = db.file_row(file_id)
    if not f or f['review_id'] != a.room:
        raise HTTPException(404, 'File not found')
    if f['note_id']:
        n = db.note(f['note_id'])
        if not n or n['deleted']:
            raise HTTPException(404, 'File not found')
    elif not a.user or f['user_key'] != user_key(a.user):
        raise HTTPException(404, 'File not found')
    safe = {'X-Content-Type-Options': 'nosniff', 'Content-Security-Policy': "sandbox; default-src 'none'", 'Cache-Control': 'private, max-age=86400'}
    mt = INLINE_TYPES.get(f['name'].rsplit('.', 1)[-1].lower())
    if mt:
        return FileResponse(f['path'], media_type=mt, headers=safe, filename=f['name'], content_disposition_type='inline')
    if 'download' not in a.perms:
        raise HTTPException(403, 'Not allowed with this link')
    return FileResponse(f['path'], media_type='application/octet-stream', headers=safe, filename=f['name'])


# ─── publish to ShotGrid ──────────────────────────────────────────────────────

_publish_locks = {}


class PublishReq(BaseModel):
    item_ids: list[int]
    note_type: Optional[str] = None
    attach_all_frames: bool = False
    statuses: dict[str, str] = {}


class Sync:
    """Records each thing as soon as it exists in ShotGrid, so a retry resumes where this stopped."""
    def __init__(self, job=None):
        self.job = job

    def note(self, note_id, sg_note_id, sg_reply_id):
        db.ex('UPDATE notes SET sg_note_id=COALESCE(?, sg_note_id), sg_reply_id=COALESCE(?, sg_reply_id), synced_at=? '
              'WHERE id=?', (sg_note_id, sg_reply_id, time.time(), note_id))
        if self.job:
            self.job['done'] += 1

    def frame(self, note_id):
        db.ex('UPDATE notes SET sg_frame_at=? WHERE id=?', (time.time(), note_id))

    def file(self, file_id, attachment_id):
        db.ex('UPDATE note_files SET sg_attachment_id=? WHERE id=?', (attachment_id, file_id))

    def path(self, file_id):
        return db.file_row(file_id)['path']


def publish_review(review, item_ids, opts, job=None):
    lock = _publish_locks.setdefault(review['id'], threading.Lock())
    if not lock.acquire(blocking=False):
        raise RuntimeError('A publish is already running for this review')

    def one(it):
        notes = db.item_notes(it['id'])
        status = (opts.get('statuses') or {}).get(str(it['id']))
        if not sg_bridge.pending_count(notes) and not status:
            return None
        if sg_bridge.pending_count(notes) and it['media_status'] != 'ready':
            media.wait_ready([it['version_id']])  # it may have been cleared, or be rebuilding
            if (db.media(it['version_id']) or {}).get('status') != 'ready':
                return {'item_id': it['id'], 'version_code': it['version_code'], 'error': 'media not ready'}
        try:
            res = sg_bridge.publish_item(review, it, notes, opts, render.render_later, Sync(job))
            if status:
                db.ex('UPDATE items SET sg_status=? WHERE id=?', (status, it['id']))
            return res
        except Exception as e:
            logger.exception('Publish failed for item %s', it['id'])
            return {'item_id': it['id'], 'version_code': it['version_code'], 'error': str(e)[:300]}

    try:
        with ThreadPoolExecutor(PUBLISH_VERSIONS, thread_name_prefix='publish') as pool:  # threads side by side; inside each, order matters
            results = [r for r in pool.map(one, [it for it in db.review_items(review['id']) if it['id'] in item_ids]) if r]
    finally:
        lock.release()
    logger.info('Published review %s: %s', review['id'], results)
    return results


_pub_jobs = {}  # a publish waits on ShotGrid per note, so it runs as a job the page follows


async def run_publish(job, a, user, req):
    try:
        job['results'] = await in_thread(publish_review, a.review, set(req.item_ids), req.model_dump(), job)
        job['status'] = 'done'
        await hub.send(a.room, {'type': 'published', 'by': user['name'],
                                'notes': [note_out(n) for n in db.review_notes(a.room)],
                                'items': items_out(None, db.review_items(a.room))})
    except Exception as e:
        logger.exception('Publish failed for review %s', a.room)
        job.update(status='error', error=str(e)[:300])
    job['at'] = time.time()


@app.post('/api/r/{token}/publish')
async def publish(token: str, req: PublishReq, request: Request):
    a = access(token, request)
    user = a.need('publish')
    now = time.time()
    for k in [k for k, j in _pub_jobs.items() if j['status'] != 'running' and now - j['at'] > 3600]:
        del _pub_jobs[k]
    ids = set(req.item_ids)
    total = sum(1 for n in db.review_notes(a.room) if n['item_id'] in ids and not n['sg_note_id'] and not n['sg_reply_id'])
    jid = secrets.token_urlsafe(16)
    job = _pub_jobs[jid] = {'room': a.room, 'status': 'running', 'done': 0, 'total': total, 'results': None, 'error': None, 'at': now}
    job['task'] = asyncio.create_task(run_publish(job, a, user, req))
    return {'id': jid}


@app.get('/api/r/{token}/publish/{job_id}')
async def publish_status(token: str, job_id: str, request: Request):
    a = access(token, request)
    a.need('publish')
    job = _pub_jobs.get(job_id)
    if not job or job['room'] != a.room:
        raise HTTPException(404, 'Publish not found')
    return {k: job[k] for k in ('status', 'done', 'total', 'results', 'error')}


# ─── exports ──────────────────────────────────────────────────────────────────

class ExportReq(BaseModel):
    kind: str
    item_ids: list[int] = []


@app.post('/api/r/{token}/export')
async def export_start(token: str, req: ExportReq, request: Request):
    a = access(token, request)
    a.need('download')
    if req.kind not in exports.KINDS:
        raise HTTPException(400, 'Unknown export')
    known = {i['id'] for i in db.review_items(a.room)}
    ids = [i for i in req.item_ids if i in known]
    if req.item_ids and not ids:
        raise HTTPException(404, 'Nothing to export')
    return {'id': await in_thread(exports.start, req.kind, a.review, ids, 'open' in a.perms)}


def export_job(a, job_id):
    a.need('download')
    job = exports.get(job_id, a.room)
    if not job:
        raise HTTPException(404, 'Export not found')
    return job


@app.get('/api/r/{token}/export/{job_id}')
async def export_status(token: str, job_id: str, request: Request):
    job = export_job(access(token, request), job_id)
    return {k: job[k] for k in ('status', 'done', 'total', 'error')}


@app.get('/api/r/{token}/export/{job_id}/file')
async def export_file(token: str, job_id: str, request: Request):
    job = export_job(access(token, request), job_id)
    if job['status'] != 'done':
        raise HTTPException(409, 'Export not ready')
    return FileResponse(job['path'], filename=job['filename'])


# ─── share links ──────────────────────────────────────────────────────────────

class ShareReq(BaseModel):
    days: int = 7
    guests: bool = False
    perms: dict = {}
    password: Optional[str] = None


@app.get('/api/r/{token}/shares')
async def list_shares(token: str, request: Request):
    a = access(token, request)
    a.need('share')
    return [share_out(s) for s in db.review_shares(a.review['id'], time.time())]


@app.post('/api/r/{token}/shares')
async def create_share(token: str, req: ShareReq, request: Request):
    a = access(token, request)
    user = a.need('share')
    if not 1 <= req.days <= MAX_SHARE_DAYS:
        raise HTTPException(400, f'Links last 1 to {MAX_SHARE_DAYS} days')
    perms = {k: bool(req.perms.get(k)) for k in GUEST_PERMS} if req.guests else {}
    password = (req.password or '') if req.guests else ''
    if password and not 4 <= len(password) <= 200:
        raise HTTPException(400, 'Passwords are 4 to 200 characters')
    pw_hash = await in_thread(hash_password, password) if password else None
    now = time.time()
    sid = db.ex('INSERT INTO shares (review_id, key, perms, guests, password_hash, created_by_name, created_at, expires_at) '
                'VALUES (?,?,?,?,?,?,?,?)',
                (a.review['id'], secrets.token_urlsafe(db.TOKEN_BYTES), json.dumps(perms), int(req.guests), pw_hash,
                 user['name'], now, now + req.days * 86400))
    return share_out(db.share_row(db.q1('SELECT * FROM shares WHERE id=?', (sid,))))


@app.delete('/api/r/{token}/shares/{share_id}')
async def revoke_share(token: str, share_id: int, request: Request):
    a = access(token, request)
    a.need('share')
    db.ex('UPDATE shares SET revoked_at=? WHERE id=? AND review_id=?', (time.time(), share_id, a.review['id']))
    for c in list(hub.rooms.get(a.room, {}).values()):  # people on that link lose access now
        if c['access'].share and c['access'].share['id'] == share_id:
            try:
                await c['ws'].close(code=4410)
            except Exception:
                pass
    return {'ok': True}


# ─── dashboard API ────────────────────────────────────────────────────────────

@app.get('/api/reviews')
async def list_reviews(request: Request, archived: int = 0):
    require_open(request)
    rows = db.q("""SELECT r.*, (SELECT COUNT(*) FROM items i WHERE i.review_id=r.id) AS item_count,
                          (SELECT COUNT(*) FROM notes n JOIN items i ON i.id=n.item_id
                           WHERE i.review_id=r.id AND n.deleted=0) AS note_count,
                          (SELECT COUNT(*) FROM notes n JOIN items i ON i.id=n.item_id
                           WHERE i.review_id=r.id AND n.deleted=0 AND n.sg_note_id IS NULL
                           AND n.sg_reply_id IS NULL) AS pending_count,
                          (SELECT MAX(n.updated_at) FROM notes n JOIN items i ON i.id=n.item_id
                           WHERE i.review_id=r.id) AS last_activity
                   FROM reviews r WHERE r.archived=? ORDER BY COALESCE(last_activity, r.created_at) DESC
                   LIMIT 300""", (archived,))
    return rows


@app.post('/api/r/{token}/archive')
async def archive(token: str, request: Request, archived: int = 1):
    require_open(request)
    db.ex('UPDATE reviews SET archived=? WHERE token=?', (archived, token))
    return {'ok': True}


# ─── group rights (dashboard, Admin group) ────────────────────────────────────

def require_admin(request):
    u = me(request)
    if not (u and u.get('v') and user_group(u) in ADMIN_GROUPS):
        raise HTTPException(403, 'Admins only')
    return u


@app.get('/api/access')
async def get_access(request: Request):
    require_admin(request)
    counts = {}
    for u in _users.values():
        if u['active']:
            counts[u['group']] = counts.get(u['group'], 0) + 1
    names = sorted({g for g in counts if g} | set(_group_rights) | ADMIN_GROUPS)
    return {'rights': [{'key': k, 'name': n} for k, n in RIGHTS],
            'groups': [{'name': g, 'users': counts.get(g, 0), 'locked': g in ADMIN_GROUPS,
                        'rights': sorted(ALL_RIGHTS) if g in ADMIN_GROUPS else _group_rights.get(g, [])} for g in names]}


class GroupRightsReq(BaseModel):
    group: str
    rights: list[str]


@app.put('/api/access')
async def put_access(req: GroupRightsReq, request: Request):
    admin = require_admin(request)
    if req.group in ADMIN_GROUPS:
        raise HTTPException(400, 'An admin group always has every right')
    _group_rights[req.group] = sorted(ALL_RIGHTS & set(req.rights))
    db.put_setting('group_rights', _group_rights)
    logger.info('%s set %s rights: %s', admin['name'], req.group, _group_rights[req.group])
    await kick(lambda u: user_group(u) == req.group)
    return {'rights': _group_rights[req.group]}


@app.get('/api/transcode')
async def get_transcode(request: Request):
    require_admin(request)
    return {**media.transcode_settings(), 'qualities': media.QUALITIES, 'widths': media.MAX_WIDTHS,
            'stored': await in_thread(media.stored)}


class TranscodeReq(BaseModel):
    crf: int
    max_width: int


@app.put('/api/transcode')
async def put_transcode(req: TranscodeReq, request: Request):
    admin = require_admin(request)
    now = media.transcode_settings()
    if req.crf != now['crf'] and req.crf not in {q['crf'] for q in media.QUALITIES}:
        raise HTTPException(400, 'Unknown quality')
    if req.max_width != now['max_width'] and req.max_width not in media.MAX_WIDTHS:
        raise HTTPException(400, 'Unknown width')
    saved = {'crf': req.crf, 'max_width': req.max_width}
    db.put_setting('transcode', saved)
    logger.info('%s set transcoding: %s', admin['name'], saved)
    return saved


@app.get('/api/transcode/status')
async def transcode_status(request: Request):
    require_admin(request)
    rooms = [r for r in hub.rooms.values() if r]
    return {**await in_thread(system.snapshot), 'people': sum(len(r) for r in rooms), 'open_reviews': len(rooms)}


@app.post('/api/transcode/clear')
async def clear_all_movies(request: Request):
    admin = require_admin(request)
    cleared = await in_thread(media.clear)
    logger.info('%s cleared all %s processed movies', admin['name'], cleared)
    return {'cleared': cleared}


@app.post('/api/r/{token}/clear-movies')
async def clear_review_movies(token: str, request: Request):
    admin = require_admin(request)
    a = access(token, request)
    cleared = await in_thread(media.clear, [i['version_id'] for i in db.review_items(a.review['id'])])
    logger.info('%s cleared %s processed movies of review %s', admin['name'], cleared, a.review['id'])
    return {'cleared': cleared}


# ─── from ShotGrid: Action Menu Items and webhooks ────────────────────────────

def create_or_update_review(user_id, project_id, project_name, playlist_id, version_ids):
    """The playlist's review, or a new one for these versions, with any new versions added."""
    user = sg_bridge.get_user(user_id) if user_id else None
    if user and 'open' not in user_rights(user):
        raise HTTPException(403, "Your ShotGrid group can't send versions to review")
    review = None
    if playlist_id:
        playlist, version_ids = sg_bridge.fetch_playlist(playlist_id)
        if not playlist:
            raise HTTPException(404, 'Playlist not found')
        name = playlist['code']
        review = db.q1('SELECT * FROM reviews WHERE playlist_id=? AND archived=0 ORDER BY id DESC LIMIT 1', (playlist_id,))
        if review:
            db.ex('UPDATE reviews SET playlist_name=? WHERE id=?', (name, review['id']))
    else:
        name = None
    versions = [v for v in sg_bridge.fetch_versions(version_ids) if (v.get('project') or {}).get('id') == project_id]
    if not versions and not review:
        raise HTTPException(400, 'No versions to review')
    if not review:
        playlist_name = name
        if not name:
            first = versions[0]['version_code']
            name = f"{datetime.now():%y%m%d} {first}" + (f' (+{len(versions) - 1})' if len(versions) > 1 else '')
        token = secrets.token_urlsafe(db.TOKEN_BYTES)
        db.ex("""INSERT INTO reviews (token, name, project_id, project_name, playlist_id, playlist_name,
                                      created_by_id, created_by_name, created_at) VALUES (?,?,?,?,?,?,?,?,?)""",
              (token, name, project_id, project_name, playlist_id, playlist_name,
               (user or {}).get('id'), (user or {}).get('name'), time.time()))
        review = db.review_by_token(token)
    return review, add_versions(review, versions), user


def reviews_for_selection(entity_type, ids):
    """Reviews holding this playlist or these versions, newest first."""
    if entity_type == 'Playlist':
        return db.q('SELECT * FROM reviews WHERE playlist_id=? AND archived=0 ORDER BY id DESC', (ids[0],))
    marks = ','.join('?' * len(ids))
    return db.q(f"""SELECT DISTINCT r.* FROM reviews r JOIN items i ON i.review_id=r.id
                    WHERE i.version_id IN ({marks}) AND r.archived=0 ORDER BY r.id DESC""", ids)


@app.post('/ami/{action}', response_class=HTMLResponse)
async def ami(action: str, request: Request, key: str = ''):
    """The buttons in ShotGrid. See hooks.py for why the request isn't taken as proof of who clicked."""
    if not AMI_KEY or not hmac.compare_digest(key, AMI_KEY):
        return HTMLResponse(hooks.page('Not allowed', '<p>This button has the wrong key. Run: reviewmeh setup-amis --apply</p>'), status_code=403)
    if action not in ('review', 'publish', 'export'):
        return HTMLResponse(hooks.page('Unknown action'), status_code=404)
    payload = await hooks.read_payload(request)
    entity_type, ids = hooks.selection(payload)
    if entity_type not in ('Playlist', 'Version') or not ids:
        return HTMLResponse(hooks.page('Nothing selected', '<p>Run this on a Playlist or on Versions.</p>'), status_code=400)
    if action == 'review':
        project_id, user_id = hooks.to_int(payload.get('project_id')), hooks.to_int(payload.get('user_id'))
        if not project_id:
            return HTMLResponse(hooks.page('No project', '<p>Run this inside a project.</p>'), status_code=400)
        try:
            review, added, user = await in_thread(create_or_update_review, user_id, project_id, payload.get('project_name'),
                                                  ids[0] if entity_type == 'Playlist' else None, ids if entity_type == 'Version' else [])
        except HTTPException as e:
            return HTMLResponse(hooks.page('Could not open the review', f'<p>{html.escape(str(e.detail))}</p>'), status_code=e.status_code)
        if added:
            await hub.send(review['id'], {'type': 'items'})
        url = f"{PUBLIC_URL}/r/{review['token']}"
        if TRUST_AMI_USER and user:
            url += f"?auth={auth_param({**user, 'v': 1})}"
        return hooks.page('Opening review…', f'<p>{html.escape(review["name"])}</p><p><a href="{html.escape(url, quote=True)}">Click here if not redirected</a></p>', url)
    # Publishing and exporting need a signed-in person: send them to the review, with the dialog open
    reviews = reviews_for_selection(entity_type, ids)
    if not reviews:
        return HTMLResponse(hooks.page('No review yet', '<p>Send this to review first.</p>'), status_code=404)
    links = [(r['name'], f"{PUBLIC_URL}/r/{r['token']}?{action}=1") for r in reviews]
    if len(links) == 1:
        return hooks.page('Opening review…', f'<p><a href="{html.escape(links[0][1], quote=True)}">Click here if not redirected</a></p>', links[0][1])
    return hooks.page('Which review?', '<ul>' + ''.join(f'<li><a href="{html.escape(u, quote=True)}">{html.escape(n)}</a></li>' for n, u in links) + '</ul>')


@app.post('/hooks/shotgrid')
async def shotgrid_webhook(request: Request):
    """Live updates: Version status changes reach open reviews, replaced movies are ingested again, cuts refresh."""
    if not WEBHOOK_SECRET:
        raise HTTPException(404)
    body = await request.body()
    if not hooks.valid_signature(WEBHOOK_SECRET, body, request.headers.get('x-sg-signature')):
        raise HTTPException(401, 'Bad signature')
    try:
        payload = json.loads(body)
    except ValueError:
        raise HTTPException(400, 'Bad JSON')
    handled = 0
    for ev in hooks.events(payload):
        kind, entity_id, attribute = ev.get('entity_type'), hooks.to_int(ev.get('entity_id')), ev.get('attribute_name')
        if not entity_id:
            continue
        if kind == 'Version' and attribute == 'sg_status_list' and ev.get('new_value'):
            db.ex('UPDATE items SET sg_status=? WHERE version_id=?', (ev['new_value'], entity_id))
            await hub.send_version(entity_id, {'type': 'status', 'version_id': entity_id, 'status': ev['new_value'], 'by': 'ShotGrid'})
            handled += 1
        elif kind == 'Version' and attribute in ('sg_uploaded_movie', 'sg_path_to_movie') and (db.media(entity_id) or {}).get('status', 'cleared') != 'cleared':
            await in_thread(media.enqueue, entity_id, True)  # a cleared proxy gets the new movie when it's next rebuilt
            handled += 1
        elif kind == 'Shot' and attribute in (SCHEMA.cut_order, SCHEMA.cut_duration, 'sg_status_list'):
            sg_bridge.forget_cuts()
            handled += 1
    return {'ok': True, 'handled': handled}


# ─── websocket ────────────────────────────────────────────────────────────────

@app.websocket('/ws/{token}')
async def ws_endpoint(ws: WebSocket, token: str):
    try:
        a = access(token, ws)
    except HTTPException as e:
        await ws.close(code=4000 + e.status_code)
        return
    user = a.user
    if not user:
        await ws.close(code=4401)
        return
    await ws.accept()
    cid = secrets.token_hex(4)
    rid = a.room
    room = hub.rooms.setdefault(rid, {})
    room[cid] = {'ws': ws, 'user': user, 'item_id': None, 'presenting': False, 'access': a}
    await ws.send_text(json.dumps({'type': 'welcome', 'cid': cid}))
    await hub.send(rid, {'type': 'presence', 'users': hub.presence(rid)})
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            t = msg.get('type')
            c = room.get(cid)
            if not c:
                break
            if a.share and a.share['expires_at'] <= time.time():
                await ws.close(code=4410)
                break
            if t == 'view':
                c['item_id'] = msg.get('item_id')
                await hub.send(rid, {'type': 'presence', 'users': hub.presence(rid)})
            elif t == 'present' and 'present' in a.perms:
                if not c['presenting']:
                    for other in room.values():
                        other['presenting'] = False
                    c['presenting'] = True
                    await hub.send(rid, {'type': 'presence', 'users': hub.presence(rid)})
                await hub.send(rid, {'type': 'present', 'from': cid, 'name': user['name'],
                                     'state': msg.get('state')}, exclude=cid)
            elif t == 'present_stop':
                c['presenting'] = False
                await hub.send(rid, {'type': 'present_stop', 'from': cid})
                await hub.send(rid, {'type': 'presence', 'users': hub.presence(rid)})
            elif t == 'pointer' and any(o['presenting'] for o in room.values()):  # cursors in frame coordinates (0..1), only while someone presents
                p = msg.get('p')
                p = [round(float(p[0]), 4), round(float(p[1]), 4)] if isinstance(p, list) and len(p) == 2 else None
                await hub.send(rid, {'type': 'pointer', 'from': cid, 'name': user['name'], 'p': p, 'v': int(msg.get('v') or 0)}, exclude=cid)
            elif t == 'draft' and c['presenting']:
                await hub.send(rid, {'type': 'draft', 'from': cid, 'name': user['name'],
                                     'version_id': msg.get('version_id'), 'frame': msg.get('frame'),
                                     'shapes': msg.get('shapes', [])[:500]}, exclude=cid)
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError, TypeError, ValueError):
        pass
    finally:
        was_presenting = room.get(cid, {}).get('presenting')
        room.pop(cid, None)
        if was_presenting:
            await hub.send(rid, {'type': 'present_stop', 'from': cid})
        await hub.send(rid, {'type': 'presence', 'users': hub.presence(rid)})
