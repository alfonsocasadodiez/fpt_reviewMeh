'use strict';
/* ReviewMeh — player, annotations, timestamped notes, live review room.
 *
 * Playback model: everything plays as a sequence of segments (S.seq). A segment is one version;
 * the sequence is one version, a multi-selection, a mini-cut (previous/current/next shot) or the
 * episode cut. Review items are segments with an item_id; cut neighbours have item_id null until
 * someone comments on them (the server then adds them to the review).
 * S.frame is local to the current segment S.cur: a 0-based index into its proxy. The UI shows
 * first_frame + index to match ShotGrid. Shapes: see draw.js.
 *
 * A note covers frames frame..frame+hold (hold -1 = until the next drawing); its drawing shows over
 * that range. Range notes are notes with a hold picked on the timeline.
 *
 * Persistence: posted notes live on the server (SQLite). Unposted drawings and comment text (S.store)
 * are saved to localStorage and mirrored to the server. Posts go through an outbox (localStorage)
 * that retries with a client_id until the server confirms, so nothing is lost or duplicated.
 */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TOKEN = decodeURIComponent(location.pathname.split('/')[2]);
const API = `/api/r/${encodeURIComponent(TOKEN)}`;
const SG_URL = APP.sg_url;
const params = new URLSearchParams(location.search);

const COLORS = ['#ff3b30', '#ff9500', '#ffcc00', '#34c759', '#00c7be', '#0a84ff', '#bf5af2', '#ffffff', '#000000'];
const SIZES = [0.003, 0.006, 0.011, 0.018];
const HOLDS = [[0, '1 frame'], [2, '3 frames'], [5, '6 frames'], [11, '12 frames'], [23, '24 frames'],
  [-1, 'Until next drawing'], [99999, 'Whole clip']];  // a note covers frame..frame+hold; 'io' = the In/Out range
const ONION = ['#ff3b30', '#34c759'];  // frames before, frames after
const modes = () => [['one', 'Version'], ['mini', 'Mini-cut'], ['cut', 'Cut']];
const CMP_MODES = [['side', 'Side'], ['wipe', 'Wipe'], ['blend', 'Blend'], ['diff', 'Diff']];

const svg = p => `<svg viewBox="0 0 16 16" width="16" height="16">${p}</svg>`;
const ICONS = {
  none: '<path d="M3 2l10 6-4.5 1L6 14z" fill="currentColor"/>',
  brush: '<path d="M14 2c-3 1-6 4-7.5 6.5l1.5 1.5C10.5 8.5 13 5 14 2z" fill="currentColor"/><path d="M6 9.5c-2 0-3 1.5-3 3 0 .8-.5 1.3-1 1.5 2.5.5 5-.5 5.3-2.8z" fill="currentColor"/>',
  pen: '<path d="M2 14l1-4 8-8 3 3-8 8z" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  line: '<path d="M2 14L14 2" stroke="currentColor" stroke-width="1.8"/>',
  arrow: '<path d="M2 14L13 3M7 3h6v6" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  rect: '<rect x="2" y="3.5" width="12" height="9" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  ellipse: '<ellipse cx="8" cy="8" rx="6" ry="4.5" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  text: '<path d="M3 3h10M8 3v10" stroke="currentColor" stroke-width="2"/>',
  eraser: '<path d="M2 11l6-7 6 5-4 4H5z M6 13h8" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  first: '<path d="M3 3v10M13 3L5 8l8 5z" fill="currentColor" stroke="currentColor" stroke-width="1.5"/>',
  last: '<path d="M13 3v10M3 3l8 5-8 5z" fill="currentColor" stroke="currentColor" stroke-width="1.5"/>',
  back: '<path d="M4 3v10M14 3L6 8l8 5z" fill="currentColor" stroke="currentColor" stroke-width="1.2"/>',
  fwd: '<path d="M12 3v10M2 3l8 5-8 5z" fill="currentColor" stroke="currentColor" stroke-width="1.2"/>',
  prevNote: '<path d="M2 2v12M14 8H5M8 4L4.5 8 8 12" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  nextNote: '<path d="M14 2v12M2 8h9M8 4l3.5 4L8 12" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  play: '<path d="M4 2.5l10 5.5-10 5.5z" fill="currentColor"/>',
  pause: '<path d="M4 2.5h3v11H4zM9 2.5h3v11H9z" fill="currentColor"/>',
  loop: '<path d="M3 9V6a2 2 0 012-2h8M11 1.5L13.5 4 11 6.5M13 7v3a2 2 0 01-2 2H3M5 9.5L2.5 12 5 14.5" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  sound: '<path d="M2 6h3l4-3v10l-4-3H2z" fill="currentColor"/><path d="M11 5.5a3.5 3.5 0 010 5M12.5 3.5a6 6 0 010 9" fill="none" stroke="currentColor" stroke-width="1.3"/>',
  muted: '<path d="M2 6h3l4-3v10l-4-3H2z" fill="currentColor"/><path d="M11 6l4 4M15 6l-4 4" stroke="currentColor" stroke-width="1.5"/>',
  export: '<path d="M8 2v8M4.5 6.5L8 10l3.5-3.5M2.5 13.5h11" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  full: '<path d="M2 6V2h4M10 2h4v4M14 10v4h-4M6 14H2v-4" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  undo: '<path d="M5 3L2 6l3 3M2 6h8a4 4 0 010 8H7" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  redo: '<path d="M11 3l3 3-3 3M14 6H6a4 4 0 000 8h3" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  clear: '<path d="M3 4h10M6 4V2.5h4V4M4.5 4l.8 10h5.4l.8-10" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  onion: '<circle cx="5.5" cy="8" r="4" fill="none" stroke="#ff6b60" stroke-width="1.4"/><circle cx="10.5" cy="8" r="4" fill="none" stroke="#4cd970" stroke-width="1.4"/><circle cx="8" cy="8" r="4" fill="none" stroke="currentColor" stroke-width="1.6"/>',
  eye: '<path d="M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5z" fill="none" stroke="currentColor" stroke-width="1.4"/><circle cx="8" cy="8" r="2.2" fill="currentColor"/>',
  eyeOff: '<path d="M1 8s2.5-5 7-5 7 5 7 5-2.5 5-7 5-7-5-7-5z" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M2 2l12 12" stroke="currentColor" stroke-width="1.6"/>',
  chat: '<path d="M2 3h12v8H7l-3 3v-3H2z" fill="none" stroke="currentColor" stroke-width="1.4"/>',
  clock: '<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M8 4.5V8l2.5 1.5" fill="none" stroke="currentColor" stroke-width="1.4"/>',
  ext: '<path d="M6 3H3v10h10v-3M9 2h5v5M14 2L7 9" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  check: '<path d="M3 8.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="1.8"/>',
  clip: '<path d="M13.5 7.3l-5.6 5.6a3.2 3.2 0 01-4.6-4.6l5.9-5.9a2.1 2.1 0 013 3l-5.7 5.7a1 1 0 01-1.5-1.5l5.2-5.2" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round"/>',
  rotate: '<path d="M13 8a5 5 0 11-1.5-3.5M12.5 1.5v3.2H9.3" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  markIn: '<path d="M7 2.5H4v11h3" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="10.5" cy="8" r="1.8" fill="currentColor"/>',
  markOut: '<path d="M9 2.5h3v11H9" fill="none" stroke="currentColor" stroke-width="1.7"/><circle cx="5.5" cy="8" r="1.8" fill="currentColor"/>',
  clearIO: '<path d="M4.5 2.5H2.5v11h2M11.5 2.5h2v11h-2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6 6l4 4M10 6l-4 4" stroke="currentColor" stroke-width="1.5"/>',
  hold: '<rect x="1.5" y="3.5" width="5" height="9" rx="1" fill="currentColor"/><path d="M8.5 8h5M11.5 5.5L14 8l-2.5 2.5" fill="none" stroke="currentColor" stroke-width="1.5"/>',
  ...Object.fromEntries([['L', 'M2 3h4v10H2z'], ['R', 'M10 3h4v10h-4z'], ['T', 'M2 3h12v3.5H2z'], ['B', 'M2 9.5h12V13H2z']].map(([k, d]) =>
    [`panel${k}`, `<rect x="1.5" y="2.5" width="13" height="11" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.2"/><path d="${d}" fill="currentColor"/>`])),
};
const TOOLS = [['none', 'Select (V)', 'v'], ['brush', 'Brush (B)', 'b'], ['pen', 'Marker (P)', 'p'], ['line', 'Line (N)', 'n'], ['arrow', 'Arrow (A)', 'a'],
  ['rect', 'Rectangle (R)', 'r'], ['ellipse', 'Ellipse (C)', 'c'], ['text', 'Text (T)', 't'], ['eraser', 'Eraser (E)', 'e']];
for (const [id, k] of [['btnFirst', 'first'], ['btnLast', 'last'], ['btnBack', 'back'], ['btnFwd', 'fwd'], ['btnPrevNote', 'prevNote'],
  ['btnNextNote', 'nextNote'], ['btnPlay', 'play'], ['btnLoop', 'loop'], ['btnMute', 'sound'], ['btnExport', 'export'], ['btnFull', 'full'],
  ['btnUndo', 'undo'], ['btnRedo', 'redo'], ['btnClear', 'clear'], ['btnAttach', 'clip'], ['btnIn', 'markIn'], ['btnOut', 'markOut'], ['btnClearIO', 'clearIO'], ['btnOnion', 'onion'], ['btnHold', 'hold'], ['btnDrawings', 'eye']]) document.getElementById(id).innerHTML = svg(ICONS[k]);
const PANELS = [['left', 'L', 'Versions'], ['top', 'T', 'Header'], ['bottom', 'B', 'Drawing tools'], ['right', 'R', 'Notes']];

const LS_STORE = `rt_store_${TOKEN}`, LS_OUTBOX = `rt_outbox_${TOKEN}`;

const S = {
  review: null, items: [], notes: [], me: null, statuses: [],
  media: {}, vinfo: {}, cuts: {}, cutOfShot: {}, picks: {},          // version_id → media / info; episode cuts
  cutFilter: null,                                                   // { task, status, only }: which version of each shot plays
  mode: 'one', anchor: null, multi: new Set(),                       // anchor: version picked in the sidebar
  seq: { kind: 'one', vids: [], segs: [], shots: null, ep: null, i: 0, total: 1 }, cur: null,
  frame: 0, playing: false, loop: localStorage.rt_loop !== '0', rate: 1, muted: false, volume: 1,
  tool: localStorage.rt_tool || 'pen', color: localStorage.rt_color || COLORS[0], size: +localStorage.rt_bsize || SIZES[+(localStorage.rt_size || 1)] || SIZES[1], opacity: +localStorage.rt_opacity || 1,
  slots: JSON.parse(localStorage.rt_slots || '[null,null,null]'),
  hold: +(localStorage.rt_hold || 0), io: null, noteIO: false, draftSel: null, tcMode: localStorage.rt_tc === '1',     // io: In/Out in sequence frames; noteIO: the note covers it
  showDrawings: localStorage.rt_show !== '0', onion: localStorage.rt_onion === '1', onionN: +(localStorage.rt_onion_n || 3),
  store: loadStore(), outbox: loadOutbox(), hist: {}, active: null, pick: null, selected: null, editing: null, hover: null,
  view: { z: 1, x: 0, y: 0, mode: 'screen' }, tl: { z: 1, x: 0 }, cmp: null, evCache: {},
  hide: JSON.parse(localStorage.rt_hide || '{}'),
  filter: 'all', presence: [], cid: null, presenting: false, following: null, optedOut: null,
  remoteDrafts: {}, lastFrame: {}, replyOpen: null, uploads: [], noteSel: new Set(), noteFrom: null, details: {}, detailDefs: {}, sgNotes: {}, fieldDefs: null, linkOpen: new Set(), linkRows: {},
};

const VIDS = [$('#vA'), $('#vB')], vC = $('#vC');
let video = VIDS[0];
const other = () => VIDS[video === VIDS[0] ? 1 : 0];
const canvas = $('#canvas'), stage = $('#stage'), ctx = canvas.getContext('2d');

// ─── helpers ────────────────────────────────────────────────────────────────
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const hue = s => { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; };
const initials = n => String(n || '?').replace(/\(guest\)/, '').trim().split(/\s+/).slice(0, 2).map(w => w[0]).join('').toUpperCase();
// ShotGrid users' pictures sit on top of their initials; the image removes itself when there is none
const avatar = (name, cls = '', id = null, title = name) => `<span class="av ${cls}" style="--h:${hue(name)}" title="${esc(title)}">${esc(initials(name))}${id ? `<img src="/api/avatar/${id}?t=${encodeURIComponent(TOKEN)}" alt="" onerror="this.remove()">` : ''}</span>`;
// Long names keep their start and end (version number); CSS trims the middle to fit, evenly from both sides
function midEllipsis(text) {
  const t = String(text ?? ''), k = Math.ceil(t.length / 2);
  return t.length < 8 ? esc(t) : `<span class="mid"><span>${esc(t.slice(0, k))}</span><span><bdi>${esc(t.slice(k))}</bdi></span></span>`;
}
const authorColor = name => `hsl(${hue(name)} 70% 55%)`;
const ago = t => { const s = Date.now() / 1000 - t; if (s < 60) return 'now'; if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`; return new Date(t * 1000).toLocaleDateString(); };
const uuid = () => crypto.randomUUID?.() || `c${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
const clone = o => JSON.parse(JSON.stringify(o));
const fps = () => S.cur?.fps || 24;
const nFrames = () => S.cur?.frame_count || Math.max(1, Math.round((video.duration || 0) * fps())) || 1;
const dispFrame = (f, seg = S.cur) => (seg?.first_frame ?? 1) + f;
const timeToFrame = t => clamp(Math.floor(t * fps() + 1e-4), 0, nFrames() - 1);
const ready = () => S.cur?.media_status === 'ready' && video.readyState >= 1;
const isMine = n => n.local || (S.me && (S.me.id ? n.author_id === S.me.id : n.author_name === S.me.name));
const pending = n => (!n.sg_note_id && !n.sg_reply_id) || !!n.sg_unsent;
const statusOf = code => S.statuses.find(s => s.code === code) || { code, name: code || '', color: '#6e7681' };
function tc(f, seg = S.cur) {
  const r = Math.max(1, Math.round(seg?.fps || 24)), s = Math.floor(f / r), p = n => String(n).padStart(2, '0');
  return `${p(Math.floor(s / 3600))}:${p(Math.floor(s / 60) % 60)}:${p(s % 60)}:${p(f % r)}`;
}

async function api(path, opts = {}) {
  const init = { credentials: 'same-origin', ...opts, headers: { 'Content-Type': 'application/json' } };
  if (opts.body && typeof opts.body !== 'string') init.body = JSON.stringify(opts.body);
  const r = await fetch(path.startsWith('/api/') ? path : API + path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (e) { /* not json */ }
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.status = r.status;
    throw err;
  }
  return r.json();
}

function toast(msg, err = false, ms = 3500) {
  const el = document.createElement('div');
  el.className = 'toast' + (err ? ' err' : '');
  el.textContent = msg;
  $('#toasts').append(el);
  setTimeout(() => el.remove(), ms);
}

// ─── persistence: drafts + outbox ───────────────────────────────────────────
// S.store = {drafts: {version_id: {frame: [shapes]}}, texts: {version_id: text}, updated_at}
function loadStore() {
  try {
    const s = JSON.parse(localStorage[LS_STORE] || 'null');
    if (s) return s;
    return { drafts: {}, texts: {}, holds: {}, updated_at: 0, legacy: JSON.parse(localStorage[`rt_drafts_${TOKEN}`] || 'null') };
  } catch (e) { return { drafts: {}, texts: {}, holds: {}, updated_at: 0 }; }
}
let storeTimer = null;
function saveStore() {
  for (const k of Object.keys(S.store.drafts)) {
    for (const f of Object.keys(S.store.drafts[k])) if (!S.store.drafts[k][f]?.length) delete S.store.drafts[k][f];
    if (!Object.keys(S.store.drafts[k]).length) delete S.store.drafts[k];
  }
  for (const k of Object.keys(S.store.texts)) if (!S.store.texts[k]) delete S.store.texts[k];
  for (const k of Object.keys(S.store.files ||= {})) if (!S.store.files[k]?.length) delete S.store.files[k];
  const holds = S.store.holds ||= {};
  for (const k of Object.keys(holds)) {
    for (const f of Object.keys(holds[k])) if (!S.store.drafts[k]?.[f]) delete holds[k][f];
    if (!Object.keys(holds[k]).length) delete holds[k];
  }
  S.store.updated_at = Date.now() / 1000;
  localStorage[LS_STORE] = JSON.stringify(S.store);
  clearTimeout(storeTimer);
  storeTimer = setTimeout(pushStore, 800);
}
async function pushStore() {
  if (!S.me || !can('comment')) return;
  const { drafts, texts, holds, files, updated_at } = S.store;
  try { await api('/drafts', { method: 'PUT', body: { data: { drafts, texts, holds, files }, updated_at } }); }
  catch (e) { clearTimeout(storeTimer); storeTimer = setTimeout(pushStore, 5000); }
}
async function pullStore() {
  if (!S.me || !can('comment')) return;
  try {
    const r = await api('/drafts');
    if (r.data && r.updated_at > (S.store.updated_at || 0)) {
      S.store = { drafts: r.data.drafts || {}, texts: r.data.texts || {}, holds: r.data.holds || {}, files: r.data.files || {}, updated_at: r.updated_at };
      localStorage[LS_STORE] = JSON.stringify(S.store);
    } else if ((S.store.updated_at || 0) > (r.updated_at || 0)) pushStore();
  } catch (e) { /* offline: keep the local copy */ }
}
function migrateLegacyDrafts() {  // v1 kept drafts keyed by item id
  const old = S.store.legacy;
  delete S.store.legacy;
  if (!old) return;
  for (const [itemId, frames] of Object.entries(old)) {
    const it = S.items.find(i => i.id === +itemId);
    if (it) S.store.drafts[it.version_id] = { ...frames, ...S.store.drafts[it.version_id] };
  }
  localStorage.removeItem(`rt_drafts_${TOKEN}`);
  saveStore();
}

const drafts = () => (S.store.drafts[S.cur?.version_id] ||= {});
// A drawing's own range (frames after its first), set by shift-clicking the timeline; else the default duration
const draftHold = (f, vid = S.cur?.version_id) => S.store.holds?.[vid]?.[f];
function setDraftHold(vid, f, h) { ((S.store.holds ||= {})[vid] ||= {})[f] = h; }
// The shapes being edited: the open note's while editing it, else the unposted drawing on frame f
function frameDraft(f = S.frame) {
  if (S.editing && f === S.editing.frame) return S.editing.shapes;
  return (S.cur && S.store.drafts[S.cur.version_id]?.[f]) || [];
}
function layer() {
  if (S.editing && S.frame === S.editing.frame) return S.editing.shapes;
  return (drafts()[S.frame] ||= []);
}

function loadOutbox() { try { return JSON.parse(localStorage[LS_OUTBOX] || '[]'); } catch (e) { return []; } }
function saveOutbox() { localStorage[LS_OUTBOX] = JSON.stringify(S.outbox); }

function postNote(body) {
  const id = uuid();
  body.client_id = id;
  const now = Date.now() / 1000;
  S.outbox.push({ body, local: { ...body, id, local: true, author_name: S.me.name, author_id: S.me.id, shapes: body.shapes || [],
    text: body.text || '', created_at: now, updated_at: now, resolved: 0, deleted: 0, hold: body.hold || 0, files: body.files || [] } });
  saveOutbox();
  flushOutbox();
}

let flushing = false, flushTimer = null;
async function flushOutbox() {
  if (flushing || !S.me) return;
  flushing = true;
  clearTimeout(flushTimer);
  try {
    while (S.outbox.length) {
      const job = S.outbox[0];
      try {
        const n = await api('/notes', { method: 'POST', body: job.body });
        S.outbox.shift(); saveOutbox();
        upsertNote(n);
        if (!job.body.item_id && !job.body.parent_id) await reload();  // the version just became a review item
      } catch (e) {
        if (e.status >= 400 && e.status < 500 && ![401, 408, 429].includes(e.status)) {
          S.outbox.shift(); saveOutbox(); restoreJob(job); toast(e.message, true);
        } else { flushTimer = setTimeout(flushOutbox, 4000); break; }
      }
    }
  } finally {
    flushing = false;
    renderNotes(); renderTimeline(); renderOverlay(); renderSidebar();
  }
}
function restoreJob({ body }) {  // rejected for good: put it back in the composer
  if (body.parent_id) return;
  const vid = body.version_id;
  if (body.text) S.store.texts[vid] = body.text;
  if (body.shapes?.length) ((S.store.drafts[vid] ||= {})[body.frame] ||= []).push(...body.shapes);
  if (body.files?.length) ((S.store.files ||= {})[vid] ||= []).push(...body.files);
  saveStore();
  if (S.cur?.version_id === vid && !S.editing) $('#noteText').value = S.store.texts[vid] || '';
}

// ─── data ───────────────────────────────────────────────────────────────────
function mediaFromItem(it) {
  return { status: it.media_status, progress: it.media_progress, error: it.media_error, fps: it.fps,
    frame_count: it.frame_count, width: it.width, height: it.height, built_at: it.built_at };
}
function applyData(d) {
  S.review = { ...d.review, studio_url: d.studio_url }; S.cutLabel = d.cut_label; S.access = d.access; S.sortFields = d.sort_fields || {}; S.items = sortItems(d.items); S.notes = d.notes; S.me = d.me; S.presence = d.presence || [];
  for (const it of S.items) if (it.media_status) S.media[it.version_id] = mediaFromItem(it);
  rebuildSegs();
  const r = S.review, title = r.playlist_name || r.name;
  document.title = `${title} — ${APP.app_name}`;
  $('#reviewName').textContent = title;
  if (r.playlist_id) $('#reviewName').href = `${SG_URL}/detail/Playlist/${r.playlist_id}`; else $('#reviewName').removeAttribute('href');
  $('#reviewMeta').textContent = r.project_name || '';
  applyAccess();
}
// What this link allows: hide what can't be used
function applyAccess() {
  for (const [id, perm] of [['btnShare', 'share'], ['btnPublish', 'publish'], ['btnAdd', 'add'], ['btnPresent', 'present'], ['btnExport', 'download']])
    $(`#${id}`).hidden = !can(perm);
  document.body.classList.toggle('no-comment', !can('comment'));
  S.fieldDefs = null; S.details = {}; S.detailDefs = {}; S.sgNotes = {}; S.linkOpen.clear(); S.linkRows = {};  // rights may have changed
  $('#rtabDetails').hidden = !can('details');
  if (S.rtab === 'details' && !can('details')) S.rtab = 'notes';
  if (!can('comment') && S.tool !== 'none') setTool('none');
}
for (const v of [...VIDS, vC]) v.addEventListener('contextmenu', e => { if (!can('download')) e.preventDefault(); });

async function reload() {
  applyData(await api(''));
  renderAll();
}

function upsertNote(n) {
  const i = S.notes.findIndex(x => x.id === n.id);
  if (i >= 0) S.notes[i] = n; else S.notes.push(n);
  S.notes.sort((a, b) => a.frame - b.frame || a.created_at - b.created_at);
}

// Notes of a segment, including posts still waiting in the outbox
function segNotes(seg = S.cur) {
  if (!seg) return [];
  const sent = new Set(S.notes.map(n => n.client_id).filter(Boolean));
  const local = S.outbox.map(j => j.local).filter(n => n.version_id === seg.version_id && !sent.has(n.client_id));
  return S.notes.filter(n => seg.item_id && n.item_id === seg.item_id && !n.deleted).concat(local);
}
const topNotes = seg => segNotes(seg).filter(n => !n.parent_id);
const itemNoteRows = id => S.notes.filter(n => n.item_id === id && !n.deleted);

function noteEnd(n, seg = S.cur) {
  const last = (seg?.frame_count || seg?.len || 1) - 1;
  if (n.hold === -1) {
    const next = topNotes(seg).filter(m => m.shapes?.length && m.frame > n.frame).map(m => m.frame);
    return next.length ? Math.max(n.frame, Math.min(...next) - 1) : last;
  }
  return n.hold > 0 ? Math.min(last, n.frame + n.hold) : n.frame;
}
// Frames read as frame numbers or timecode (the toggle next to the frame box)
const fmtF = (f, seg = S.cur) => S.tcMode ? tc(f, seg) : `f${dispFrame(f, seg)}`;
const rangeLabel = (a, b, seg = S.cur) => `${fmtF(a, seg)}${b > a ? `–${S.tcMode ? tc(b, seg) : dispFrame(b, seg)}` : ''}`;
const can = perm => !!S.access?.perms?.includes(perm);
const noteLabel = (n, seg = S.cur) => rangeLabel(n.frame, noteEnd(n, seg), seg);

// ─── sequence model ─────────────────────────────────────────────────────────
// Browsers keep a proxy for an hour: the time it was made is part of its url, so a movie made again is a new address
const proxyUrl = (vid, m) => `${API}/media/${vid}/proxy.mp4${m?.built_at ? `?b=${m.built_at}` : ''}`;
function segFor(vid) {
  const it = S.items.find(i => i.version_id === vid), m = S.media[vid] || {};
  const ready = m.status === 'ready';
  return { ...S.vinfo[vid], ...(it || {}), version_id: vid, item_id: it ? it.id : null,
    media_status: m.status, media_progress: m.progress, media_error: m.error, fps: m.fps, frame_count: m.frame_count,
    width: m.width, height: m.height,
    proxy_url: ready ? proxyUrl(vid, m) : null, thumb_url: ready ? `${API}/media/${vid}/thumb.jpg` : null };
}
function rebuildSegs() {
  const q = S.seq;
  let off = 0;
  q.segs = q.vids.map(vid => {
    const s = segFor(vid);
    s.offset = off;
    s.len = s.frame_count || s.cut_duration || 48;
    off += s.len;
    return s;
  });
  q.total = Math.max(1, off);
  S.cur = q.segs[q.i] || null;
  clampTl();
}
const gFrame = () => (S.cur?.offset || 0) + S.frame;
function locate(g) {
  const segs = S.seq.segs;
  let i = segs.findIndex(s => g < s.offset + s.len);
  if (i < 0) i = segs.length - 1;
  return { i, f: clamp(g - segs[i].offset, 0, segs[i].len - 1) };
}

function setSeq(kind, vids, i = 0, frame, opts = {}) {
  if (S.cur) S.lastFrame[S.cur.version_id] = S.frame;
  S.seq = { kind, vids, segs: [], shots: opts.shots || null, ep: opts.ep ?? null, i: clamp(i, 0, Math.max(0, vids.length - 1)), total: 1 };
  S.io = null; S.noteIO = false; S.tl = { z: 1, x: 0 };
  rebuildSegs();
  ensureSeqMedia();
  enterSeg(frame, opts);
}

function goSeg(i, frame, opts = {}) {
  if (i === S.seq.i && S.cur) {
    if (frame != null) seek(frame);
    if (opts.note !== undefined) { S.selected = opts.note; renderNotes(); renderTimeline(); }
    if (opts.play) playNow();
    return;
  }
  if (S.cur) S.lastFrame[S.cur.version_id] = S.frame;
  S.seq.i = i;
  S.cur = S.seq.segs[i];
  enterSeg(frame, opts);
}

function enterSeg(frame, { play = false, note } = {}) {
  const changed = S.loadedVid !== S.cur?.version_id;
  S.pick = null;
  if (changed) {
    if (S.editing) cancelEdit(false);
    S.selected = note ?? null;
    if (S.cmp && S.cmp.vid0 !== S.cur?.version_id) retargetCompare();
    $('#noteText').value = (S.cur && S.store.texts[S.cur.version_id]) || '';
  } else if (note !== undefined) S.selected = note;
  S.frame = frame ?? S.lastFrame[S.cur?.version_id] ?? 0;
  loadVideo(play);
  wsSend({ type: 'view', item_id: S.cur?.item_id ?? null });
  updateURL();
  renderAll();
  broadcastPresent();
}

// Mode = how the sidebar pick plays: alone, with its neighbouring shots, or inside the episode cut
async function openMode(mode, vid = S.anchor, opts = {}) {
  S.mode = mode;
  S.anchor = vid;
  S.multi.clear();
  if (!vid) { setSeq('one', [], 0); return; }
  const a = segFor(vid);
  if (mode !== 'one' && a.entity_type === 'Shot') {
    try {
      const cut = await loadCut(a.entity_id);
      const idx = cut.shots.findIndex(s => s.id === a.entity_id);
      if (idx >= 0) {
        let shots = mode === 'mini' ? cut.shots.slice(Math.max(0, idx - 1), idx + 2) : cut.shots;
        if (mode === 'cut' && S.cutFilter?.only) shots = shots.filter(s => s.id === a.entity_id || cutMatches(s, S.cutFilter).length);
        const vids = shots.map(s => (s.id === a.entity_id ? vid : pickVersion(s, a.task_name)));
        setSeq(mode, vids, shots.findIndex(s => s.id === a.entity_id), opts.frame, { ...opts, shots, ep: cut.episode.id });
        return;
      }
    } catch (e) { toast(e.message, true); }
    S.mode = 'one';
  }
  setSeq('one', [vid], 0, opts.frame, opts);
}

async function loadCut(shotId, fresh = false) {
  const ep = S.cutOfShot[shotId];
  if (ep && S.cuts[ep] && !fresh) return S.cuts[ep];
  const cut = await api(`/cut?shot_id=${shotId}${fresh ? '&fresh=1' : ''}`);
  for (const sh of cut.shots) {
    S.cutOfShot[sh.id] = cut.episode.id;
    for (const v of sh.versions) S.vinfo[v.version_id] = { ...v, cut_duration: sh.cut_duration };
  }
  for (const [vid, m] of Object.entries(cut.media)) S.media[vid] = m;
  S.cuts[cut.episode.id] = cut;
  return cut;
}

// Version for a shot: explicit pick, else the cut filter, else the anchor's task, else the latest pipeline step
function pickVersion(shot, task) {
  const vs = shot.versions, forced = S.picks[shot.id];
  if (forced && vs.some(v => v.version_id === forced)) return forced;
  if (S.cutFilter) return filteredVersion(shot, S.cutFilter);
  return (vs.find(v => v.task_name === task) || latestStage(vs)).version_id;
}
const latestStage = vs => [...vs].sort((a, b) => (b.task_order ?? -1) - (a.task_order ?? -1) || b.created_at - a.created_at)[0];

// Cut filter: task and status narrow what a shot can play; with neither, each shot plays the latest of everything
const cutMatches = (shot, f) => shot.versions.filter(v => (!f.task || v.task_name === f.task) && (!f.status || v.sg_status === f.status));
function filteredVersion(shot, f) {
  const hits = cutMatches(shot, f);
  if (hits.length) return latestStage(hits).version_id;
  // Nothing matches: the shot keeps its place in the cut, on that task or else its nearest earlier step
  const vs = shot.versions, onTask = f.task && vs.find(v => v.task_name === f.task);
  if (onTask) return onTask.version_id;
  const order = S.cuts[S.cutOfShot[shot.id]]?.tasks.find(t => t.name === f.task)?.order;
  const lower = order == null ? [] : vs.filter(v => (v.task_order ?? -1) <= order);
  return latestStage(lower.length ? lower : vs).version_id;
}
function applyCutFilter(f) {
  const q = S.seq, cut = S.cuts[q.ep];
  if (!q.shots || !cut) return;
  local();
  S.cutFilter = f;
  for (const sh of cut.shots) delete S.picks[sh.id];
  const base = q.kind === 'cut' ? cut.shots : q.shots, cur = q.shots[q.i];
  let shots = base;
  if (q.kind === 'cut' && f.only) {
    shots = base.filter(s => cutMatches(s, f).length);
    if (!shots.length) { toast('No shots match, so the whole cut stays'); shots = base; }
  }
  const vids = shots.map(s => pickVersion(s));
  if (shots.map(s => s.id).join() === q.shots.map(s => s.id).join()) {
    q.shots = shots;
    setSegVersions(vids.map((v, i) => [i, v]));
  } else {
    // The shot being watched stays if it can, else the next one in cut order that's left
    const pos = s => cut.shots.findIndex(x => x.id === s.id);
    let i = shots.findIndex(s => s.id === cur.id);
    const same = i >= 0;
    if (!same) { i = shots.findIndex(s => pos(s) > pos(cur)); if (i < 0) i = shots.length - 1; }
    setSeq(q.kind, vids, i, same ? S.frame : 0, { shots, ep: q.ep });
  }
  S.anchor = S.seq.vids[S.seq.i];
  updateURL();
}

function setSegVersions(changes) {  // [[segIndex, version_id]]
  const q = S.seq;
  for (const [i, vid] of changes) { q.vids[i] = vid; if (q.shots) S.picks[q.shots[i].id] = vid; }
  rebuildSegs();
  ensureSeqMedia();
  if (changes.some(([i]) => i === q.i)) enterSeg(S.frame); else { renderTimeline(); renderComp(); }
  broadcastPresent();
}

async function ensureSeqMedia() {
  const q = S.seq, order = [];
  for (let k = 0; k < q.segs.length; k++) for (const j of [q.i + k, q.i - k]) if (j >= 0 && j < q.segs.length && !order.includes(j)) order.push(j);
  const want = order.map(j => q.segs[j]).filter(s => !['ready', 'error', 'nomedia'].includes(s.media_status)).map(s => s.version_id);
  if (!want.length) return;
  try {
    const rows = await api('/media', { method: 'POST', body: { version_ids: want } });
    for (const [vid, m] of Object.entries(rows)) S.media[vid] = m;
    mediaChanged(want);
  } catch (e) { /* the media websocket events will still arrive */ }
}

function mediaChanged(vids) {
  const curReadyBefore = !!S.cur?.proxy_url;
  rebuildSegs();
  if (S.cur && vids.includes(S.cur.version_id) && !curReadyBefore && S.cur.proxy_url) loadVideo(false);
  else preloadNext();
  if (S.cmp && vids.includes(S.cmp.vid)) loadCompareVideo();
  renderSidebar(); renderStageMsg(); renderTimeline(); renderComp(); renderCompareBar();
}

// ─── playback: two <video> elements, the next segment preloads in the hidden one ──
function loadVideo(play) {
  const url = S.cur?.proxy_url;
  S.loadedVid = S.cur?.version_id;
  if (!url) {
    video.pause(); video.removeAttribute('src'); video.dataset.url = ''; video.load();
    return;
  }
  const o = other();
  if (o.dataset.url === url && video.dataset.url !== url) {
    const old = video;
    video = o;
    video.classList.add('on'); old.classList.remove('on');
    old.pause();
  } else if (video.dataset.url !== url) {
    video.dataset.url = url;
    video.src = url;
  }
  applyAudio();
  layout();
  if (video.readyState >= 1) seek(S.frame, { broadcast: false });
  if (play) playNow();
  preloadNext();
}

function nextReady(i, wrap) {
  const segs = S.seq.segs;
  for (let k = 1; k <= segs.length; k++) {
    if (i + k >= segs.length && !wrap) return null;
    const j = (i + k) % segs.length;
    if (segs[j].proxy_url) return j;
  }
  return null;
}

function preloadNext() {
  const wrapIO = S.io && S.loop && S.io.b < (S.cur?.offset || 0) + (S.cur?.len || 0);
  const j = wrapIO ? locate(S.io.a).i : nextReady(S.seq.i, S.loop);
  const url = j != null && j !== S.seq.i ? S.seq.segs[j].proxy_url : null;
  const o = other();
  if (url && o.dataset.url !== url) { o.dataset.url = url; o.src = url; o.currentTime = 0; }
}

function applyAudio() {
  for (const v of VIDS) {
    v.muted = S.muted; v.volume = S.volume;
    v.defaultPlaybackRate = v.playbackRate = S.rate;
    v.loop = S.loop && S.seq.segs.length === 1 && !S.io;
  }
  vC.playbackRate = S.rate;
}

function seek(f, { broadcast = true } = {}) {
  if (!S.cur) return;
  f = clamp(Math.round(f), 0, nFrames() - 1);
  if (f !== S.frame) S.pick = null;
  S.frame = f;
  if (video.readyState >= 1) {
    let t = (f + 0.5) / fps();
    if (video.duration && t > video.duration) t = Math.max(0, video.duration - 0.001);
    video.currentTime = t;
  }
  cmpSeek();
  onFrame();
  if (broadcast) broadcastPresentThrottled();
}

function step(d) {
  if (S.playing) video.pause();
  const g = gFrame() + d;
  if (S.seq.segs.length > 1 && (S.frame + d < 0 || S.frame + d >= nFrames())) {
    const { i, f } = locate(clamp(g, 0, S.seq.total - 1));
    goSeg(i, f);
  } else seek(S.frame + d);
}

function playNow() { video.play().catch(() => toast('Click the video to start playback')); }
function togglePlay() {
  if (!ready()) return;
  if (!video.paused) return video.pause();
  const g = gFrame();
  if (S.io && (g < S.io.a || g >= S.io.b)) return jumpGlobal(S.io.a, true);
  if (S.frame >= nFrames() - 1 && S.seq.segs.length === 1 && !S.loop) seek(0, { broadcast: false });
  playNow();
}
function jumpGlobal(g, play) {
  const { i, f } = locate(g);
  S.ioJump = performance.now();
  if (i === S.seq.i) { seek(f, { broadcast: false }); if (play && video.paused) playNow(); }
  else goSeg(i, f, { play });
}
// playback reached Out: loop back to In, or stop on Out
function ioEnd() {
  if (S.loop) return jumpGlobal(S.io.a, true);
  video.pause();
  jumpGlobal(S.io.b, false);
}

function onEnded() {
  if (S.io && S.io.b < S.cur.offset + S.cur.len) return ioEnd();
  const j = nextReady(S.seq.i, S.loop);
  if (j != null && j !== S.seq.i) return goSeg(j, 0, { play: true });
  if (S.loop) { seek(0, { broadcast: false }); return playNow(); }
  S.playing = false;
  $('#btnPlay').innerHTML = svg(ICONS.play);
  seek(nFrames() - 1, { broadcast: false });
  broadcastPresent();
}

function startTick() {
  const v = video;
  const tick = (now, md) => {
    if (!S.playing || v !== video) return;
    S.frame = timeToFrame(md ? md.mediaTime : v.currentTime);
    if (S.io && gFrame() >= S.io.b && performance.now() - (S.ioJump || 0) > 150) { ioEnd(); if (!S.playing) return; }
    onFrame();
    if ('requestVideoFrameCallback' in v) v.requestVideoFrameCallback(tick); else requestAnimationFrame(() => tick());
  };
  if ('requestVideoFrameCallback' in v) v.requestVideoFrameCallback(tick); else requestAnimationFrame(() => tick());
}

for (const v of VIDS) {
  v.addEventListener('loadedmetadata', () => { if (v === video) { layout(); seek(S.frame, { broadcast: false }); } });
  v.addEventListener('play', () => {
    if (v !== video) return;
    S.playing = true;
    startTick();
    $('#btnPlay').innerHTML = svg(ICONS.pause);
    cmpPlay();
    broadcastPresent();
  });
  v.addEventListener('pause', () => {
    if (v !== video || v.ended) return;
    S.playing = false;
    $('#btnPlay').innerHTML = svg(ICONS.play);
    vC.pause();
    seek(timeToFrame(v.currentTime), { broadcast: false });  // snap to the frame boundary
    broadcastPresent();
  });
  v.addEventListener('ended', () => { if (v === video) onEnded(); });
}

function jumpNote(dir) {
  const frames = [...new Set(topNotes().map(n => n.frame).concat(Object.keys(S.store.drafts[S.cur?.version_id] || {}).map(Number)))].sort((a, b) => a - b);
  const target = dir > 0 ? frames.find(f => f > S.frame) : frames.reverse().find(f => f < S.frame);
  if (target != null) { if (S.playing) video.pause(); seek(target); selectAtFrame(); }
}
function selectAtFrame() {
  const n = topNotes().find(x => x.frame === S.frame);
  S.selected = n ? n.id : null;
  renderNotes(); renderTimeline();
  if (n) $(`.note[data-id="${n.id}"]`)?.scrollIntoView({ block: 'nearest' });
}

// called on every frame change (playback or seek): keep cheap
function onFrame() {
  if (document.activeElement !== $('#frameInput')) $('#frameInput').value = S.cur ? (S.tcMode ? tc(S.frame) : dispFrame(S.frame)) : '';
  $('#tc').textContent = S.cur ? (S.tcMode ? dispFrame(S.frame) : tc(S.frame)) : '';
  $('#playhead').style.left = tlPos(gFrame() + 0.5);
  followPlayhead();
  for (const el of $$('.note[data-frame]')) el.classList.toggle('at-frame', S.frame >= +el.dataset.frame && S.frame <= +el.dataset.end);
  if (S.playing && S.cmp && !vC.paused && vC.readyState >= 1) {
    const m = S.media[S.cmp.vid];
    if (Math.abs(Math.floor(vC.currentTime * (m?.fps || 24)) - S.frame) > 1) cmpSeek();
  }
  renderOverlay();
  if (!S.playing) renderFrameChip();
}

// ─── layout, zoom, compare ──────────────────────────────────────────────────
const frameSize = () => [S.cur?.width || video.videoWidth || 16, S.cur?.height || video.videoHeight || 9];
const stageWidth = () => (S.cmp?.mode === 'side' ? stage.clientWidth / 2 : stage.clientWidth);
// S.view.z is relative to the whole frame fitting the stage. mode keeps a fit or a size true through resizes and shot changes:
// 'screen', 'width', 'height' or a percentage, where 100 is one movie pixel on one screen pixel. Free zoom has none.
function zoomForMode(mode) {
  const [vw, vh] = frameSize(), cw = stageWidth(), H = stage.clientHeight, fit = Math.min(cw / vw, H / vh);
  if (mode === 'width') return cw / vw / fit;
  if (mode === 'height') return H / vh / fit;
  if (mode === 'screen') return 1;
  return mode / 100 / (window.devicePixelRatio || 1) / fit;
}
function setView(mode) {
  S.view = { z: zoomForMode(mode), x: 0, y: 0, mode };
  layout();
}
function contentRect() {
  const W = stage.clientWidth, H = stage.clientHeight, cw = stageWidth();
  const [vw, vh] = frameSize();
  if (S.view.mode != null) S.view.z = zoomForMode(S.view.mode);
  const s = Math.min(cw / vw, H / vh) * S.view.z, w = vw * s, h = vh * s;
  return { x: (cw - w) / 2 + S.view.x, y: (H - h) / 2 + S.view.y, w, h };
}
const place = (el, R) => Object.assign(el.style, { left: `${R.x}px`, top: `${R.y}px`, width: `${R.w}px`, height: `${R.h}px` });

function layout() {
  const dpr = window.devicePixelRatio || 1, W = stage.clientWidth, H = stage.clientHeight;
  if (canvas.width !== Math.round(W * dpr) || canvas.height !== Math.round(H * dpr)) {
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const R = contentRect(), fc = $('#frameC'), wipe = $('#wipe');
  place($('#frameA'), R);
  fc.hidden = !S.cmp;
  wipe.hidden = S.cmp?.mode !== 'wipe';
  if (S.cmp) {
    const m = S.cmp.mode;
    place(fc, m === 'side' ? { ...R, x: R.x + W / 2 } : R);
    fc.style.opacity = m === 'blend' ? S.cmp.opacity : 1;
    fc.style.mixBlendMode = m === 'diff' ? 'difference' : 'normal';
    const g = m === 'wipe' ? wipeGeom(R) : null;
    fc.style.clipPath = g ? g.clip : 'none';
    if (g) {
      wipe.hidden = !g.line;
      if (g.line) {
        const [p, q] = g.line, len = Math.hypot(q[0] - p[0], q[1] - p[1]);
        place(wipe, { x: R.x + (p[0] + q[0]) / 2 - 1, y: R.y + (p[1] + q[1]) / 2 - len / 2, w: 2, h: len });
        wipe.style.transform = `rotate(${S.cmp.wipeA || 0}deg)`;
      }
    }
  }
  $('#btnZoom').textContent = `${Math.round(R.w * (window.devicePixelRatio || 1) / frameSize()[0] * 100)}%`;
  $('#btnZoom').hidden = !S.cur;
  placePointer();
  renderOverlay();
}
new ResizeObserver(layout).observe(stage);

const ZOOM_MIN = 10, ZOOM_MAX = 800, ZOOM_SIZES = [25, 50, 100, 200, 400];
function zoomAt(factor, cx = stage.clientWidth / 2, cy = stage.clientHeight / 2) {
  const R = contentRect(), was = S.view.z;
  let z = clamp(was * factor, Math.min(1, zoomForMode(ZOOM_MIN)), Math.max(16, zoomForMode(ZOOM_MAX)));
  if ((was - 1) * (z - 1) < 0) z = 1;  // zooming through the whole frame stops there first
  if (z === 1) return setView('screen');
  if (z < 1) { S.view = { z, x: 0, y: 0 }; return layout(); }  // smaller than the stage: nothing to pan
  const k = z / was, cw = stageWidth();
  S.view = { z, x: cx - (cx - R.x) * k - (cw - R.w * k) / 2, y: cy - (cy - R.y) * k - (stage.clientHeight - R.h * k) / 2 };
  layout();
}
stage.addEventListener('wheel', e => {
  if (!S.cur) return;
  e.preventDefault();
  const r = stage.getBoundingClientRect();
  zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015)), e.clientX - r.left, e.clientY - r.top);
}, { passive: false });
$('#btnZoom').addEventListener('click', e => {
  const r = e.currentTarget.getBoundingClientRect(), mode = S.view.mode;
  const entry = (m, html) => ({ html, on: mode === m, run: () => setView(m) });
  openMenu(r.left, r.top, [entry('screen', 'Fit to screen'), entry('width', 'Fit to width'), entry('height', 'Fit to height'),
    ...ZOOM_SIZES.map(p => entry(p, `${p}%`))], { narrow: true });
});

// Versions of a segment's shot/asset, newest first: the episode cut already has them, else ask once
async function entityVersions(seg) {
  const sh = S.seq.shots?.[S.seq.segs.indexOf(seg)];
  if (sh?.id === seg.entity_id) return sh.versions;
  const k = `${seg.entity_type}:${seg.entity_id}`;
  if (!S.evCache[k]) {
    const list = await api(`/versions?entity_type=${encodeURIComponent(seg.entity_type)}&entity_id=${seg.entity_id}`);
    for (const v of list) { S.vinfo[v.version_id] = { ...S.vinfo[v.version_id], ...v }; if (v.media) S.media[v.version_id] = v.media; }
    S.evCache[k] = list;
  }
  return S.evCache[k];
}
// Compare target: the newest version of the picked task, else the take before this one
function compareDefault(list, seg, task) {
  const others = list.filter(v => v.version_id !== seg.version_id);
  if (task) { const v = others.find(x => x.task_name === task); if (v) return v; }
  const t0 = list.find(v => v.version_id === seg.version_id)?.created_at ?? Infinity, older = others.filter(v => v.created_at < t0);
  return older.find(v => v.task_name === seg.task_name) || older[0] || others[0] || null;
}

async function openCompare() {
  const cur = S.cur;
  if (!cur?.entity_id) return;
  let list;
  try { list = await entityVersions(cur); } catch (e) { return toast(e.message, true); }
  if (!list.some(v => v.version_id !== cur.version_id)) return toast('No other versions of this shot');
  S.cmp = { vid0: null, list: [], vid: null, task: null, mode: localStorage.rt_cmp || 'wipe', opacity: 0.5, wipe: 0.5, wipeA: +(localStorage.rt_wipe_a || 0) };
  await retargetCompare();
  renderHeader();
}
// The shown segment changed: keep comparing, against the same kind of version of the new shot
async function retargetCompare() {
  const c = S.cmp, cur = S.cur;
  if (!c || !cur) return;
  c.vid0 = cur.version_id;
  const key = `${cur.entity_type}:${cur.entity_id}`;
  if (!S.evCache[key] && !S.seq.shots) setCompareVersion(null);  // blank while the list loads
  let list = [];
  try { if (cur.entity_id) list = await entityVersions(cur); } catch (e) { /* nothing to compare against */ }
  if (S.cmp !== c || S.cur !== cur) return;
  c.list = list.filter(v => v.version_id !== cur.version_id);
  setCompareVersion(compareDefault(list, cur, c.task)?.version_id ?? null);
  if (S.seq.shots) {  // warm up the next shots' compare media
    const next = [1, 2].map(d => S.seq.segs[S.seq.i + d]).filter(Boolean)
      .map(sg => compareDefault(S.seq.shots[S.seq.segs.indexOf(sg)]?.versions || [], sg, c.task)?.version_id).filter(Boolean);
    if (next.length) api('/media', { method: 'POST', body: { version_ids: next } }).then(rows => Object.assign(S.media, rows)).catch(() => {});
  }
}
async function setCompareVersion(vid, picked = false) {
  const c = S.cmp;
  c.vid = vid;
  if (picked) { const v = c.list.find(x => x.version_id === vid); c.task = v && v.task_name !== S.cur?.task_name ? v.task_name : null; }
  loadCompareVideo();
  renderCompareBar(); layout();
  if (!vid || S.media[vid]?.status === 'ready') return;
  try {
    const rows = await api('/media', { method: 'POST', body: { version_ids: [vid] } });
    Object.assign(S.media, rows);
    loadCompareVideo(); renderCompareBar();
  } catch (e) { toast(e.message, true); }
}
function loadCompareVideo() {
  if (!S.cmp) return;
  const m = S.media[S.cmp.vid], url = S.cmp.vid && m?.status === 'ready' ? proxyUrl(S.cmp.vid, m) : '';
  if (vC.dataset.url === url) return;
  vC.dataset.url = url;
  if (url) vC.src = url; else { vC.removeAttribute('src'); vC.load(); }
}
function closeCompare() {
  S.cmp = null;
  vC.pause(); vC.removeAttribute('src'); vC.dataset.url = ''; vC.load();
  renderCompareBar(); layout(); renderHeader();
}
function cmpSeek() {
  if (!S.cmp || vC.readyState < 1) return;
  const m = S.media[S.cmp.vid] || {};
  vC.currentTime = (clamp(S.frame, 0, (m.frame_count || 1) - 1) + 0.5) / (m.fps || 24);
}
function cmpPlay() { if (S.cmp && vC.readyState >= 1) { vC.playbackRate = S.rate; cmpSeek(); vC.play().catch(() => {}); } }
vC.addEventListener('loadedmetadata', () => { cmpSeek(); if (S.playing) cmpPlay(); });

function renderCompareBar() {
  const bar = $('#cmpBar');
  if (!S.cmp) { bar.hidden = true; bar.innerHTML = ''; bar.dataset.sig = ''; return; }
  const m = S.media[S.cmp.vid], byTask = {};
  for (const v of S.cmp.list) (byTask[v.task_name || '—'] ||= []).push(v);
  const prog = !m || m.status === 'ready' ? '' : m.status === 'error' || m.status === 'nomedia' ? '<span class="err">⚠</span>'
    : `<span class="muted mono">${Math.round((m.progress || 0) * 100)}%</span>`;
  bar.hidden = false;
  const sig = JSON.stringify([S.cmp.vid, S.cmp.mode, S.cmp.list.map(v => [v.version_id, v.sg_status]), S.statuses.length]);
  if (bar.dataset.sig === sig) { $('#cmpProg').innerHTML = prog; return; }
  bar.dataset.sig = sig;
  bar.innerHTML = `<select id="cmpVer" ${S.cmp.list.length ? '' : 'disabled'}>${S.cmp.vid ? '' : '<option>—</option>'}${Object.entries(byTask).map(([t, vs]) => `<optgroup label="${esc(t)}">${vs.map(v =>
      `<option value="${v.version_id}" ${v.version_id === S.cmp.vid ? 'selected' : ''}>${esc(v.version_code)} · ${esc(statusOf(v.sg_status).name)}</option>`).join('')}</optgroup>`).join('')}</select>
    <span id="cmpProg">${prog}</span>
    <div class="seg">${CMP_MODES.map(([k, l]) => `<button data-cmode="${k}" class="${S.cmp.mode === k ? 'active' : ''}">${l}</button>`).join('')}</div>
    ${S.cmp.mode === 'wipe' ? `<button class="icon" id="cmpRot">${svg(ICONS.rotate)}</button>` : ''}
    ${S.cmp.mode === 'blend' ? `<input type="range" id="cmpOpacity" min="0" max="1" step="0.01" value="${S.cmp.opacity}" style="--p:${S.cmp.opacity * 100}%">` : ''}
    <button class="icon" id="cmpClose" title="Close">×</button>`;
}
$('#cmpBar').addEventListener('change', e => { if (e.target.id === 'cmpVer') setCompareVersion(+e.target.value, true); });
$('#cmpBar').addEventListener('input', e => { if (e.target.id === 'cmpOpacity') { S.cmp.opacity = +e.target.value; fillRange(e.target); layout(); } });
$('#cmpBar').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.id === 'cmpClose') return closeCompare();
  if (b.id === 'cmpRot') return setWipeAngle(Math.floor((S.cmp.wipeA || 0) / 45) * 45 + 45);
  if (b.dataset.cmode) { S.cmp.mode = localStorage.rt_cmp = b.dataset.cmode; renderCompareBar(); layout(); }
});
// The wipe is a line at any angle: the far side shows the compare version. wipe 0..1 slides it across the frame.
function wipeGeom(R) {
  const th = (S.cmp.wipeA || 0) * Math.PI / 180, n = [Math.cos(th), Math.sin(th)], c = [R.w / 2, R.h / 2];
  const L = Math.abs(R.w * n[0]) + Math.abs(R.h * n[1]), d = (S.cmp.wipe - 0.5) * L;
  const side = p => (p[0] - c[0]) * n[0] + (p[1] - c[1]) * n[1] - d;
  const box = [[0, 0], [R.w, 0], [R.w, R.h], [0, R.h]], poly = [], cut = [];
  box.forEach((p, i) => {
    const q = box[(i + 1) % 4], a = side(p), b = side(q);
    if (a >= 0) poly.push(p);
    if (a * b < 0 || a === 0) {
      const t = a / (a - b), x = [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t];
      if (a * b < 0) poly.push(x);
      cut.push(x);
    }
  });
  return { n, c, L, clip: poly.length > 2 ? `polygon(${poly.map(p => `${p[0]}px ${p[1]}px`).join(',')})` : 'inset(100%)', line: cut.length >= 2 ? [cut[0], cut[cut.length - 1]] : null };
}
function setWipeAngle(a) {
  S.cmp.wipeA = ((a % 360) + 360) % 360;
  localStorage.rt_wipe_a = S.cmp.wipeA;
  layout();
}
$('#wipe').addEventListener('pointerdown', e => {
  const el = e.currentTarget, turning = !!e.target.closest('i'), r = stage.getBoundingClientRect();
  e.target.setPointerCapture(e.pointerId);  // on the target itself: a double-click still lands on the handle
  const R0 = contentRect(), line = wipeGeom(R0).line, mid = line ? [R0.x + (line[0][0] + line[1][0]) / 2, R0.y + (line[0][1] + line[1][1]) / 2] : [R0.x + R0.w / 2, R0.y + R0.h / 2];
  const move = ev => {
    const x = ev.clientX - r.left, y = ev.clientY - r.top;
    if (turning) {  // the round handle turns it: Shift snaps to 15°, otherwise it settles on 45° steps
      const deg = Math.atan2(x - mid[0], mid[1] - y) * 180 / Math.PI, step = ev.shiftKey ? 15 : 45, near = Math.round(deg / step) * step;
      return setWipeAngle(ev.shiftKey || Math.abs(deg - near) < 3 ? near : deg);
    }
    const R = contentRect(), g = wipeGeom(R);
    S.cmp.wipe = clamp(((x - R.x - g.c[0]) * g.n[0] + (y - R.y - g.c[1]) * g.n[1]) / g.L + 0.5, 0, 1);
    layout();
  };
  el.addEventListener('pointermove', move);
  el.addEventListener('pointerup', () => el.removeEventListener('pointermove', move), { once: true });
});
$('#wipe').addEventListener('dblclick', e => { if (e.target.closest('i')) setWipeAngle(0); });

// ─── overlay: drawings, onion skin, selection ───────────────────────────────
const draftFrames = () => Object.keys(S.store.drafts[S.cur?.version_id] || {}).map(Number).filter(k => S.store.drafts[S.cur.version_id][k]?.length);
// A note held "until next drawing" also gives way to an unposted drawing
function visibleNotes(f) {
  const df = draftFrames();
  return topNotes().filter(n => n.shapes?.length && n.id !== S.editing?.id && f >= n.frame && f <= noteEnd(n)
    && !(n.hold === -1 && df.some(k => k > n.frame && k <= f)));
}
function shapesStartingAt(f) {
  return topNotes().filter(n => n.frame === f && n.shapes?.length && n.id !== S.editing?.id).flatMap(n => n.shapes).concat(frameDraft(f));
}
// With hold on, an unposted drawing stays up until the next drawing: [start frame, next drawing frame | Infinity]
function draftHolds() {
  if (!S.cur) return [];
  const df = draftFrames().sort((a, b) => a - b), posted = topNotes().filter(n => n.shapes?.length).map(n => n.frame);
  return df.filter(k => (draftHold(k) ?? S.hold) === -1).map(k => [k, Math.min(Infinity, ...df.concat(posted).filter(x => x > k))]);
}
function heldDraft(f) {
  if (frameDraft(f).length) return null;
  const ranged = draftFrames().filter(k => k < f && draftHold(k) > 0 && f <= k + draftHold(k)).pop();
  if (ranged != null) return S.store.drafts[S.cur.version_id][ranged];
  const h = draftHolds().find(([k, next]) => k < f && f < next);
  return h ? S.store.drafts[S.cur.version_id][h[0]] : null;
}
const eraserRadius = R => 4 + S.size * R.h * 2.5;

function renderOverlay() {
  syncSliders();
  ctx.clearRect(0, 0, stage.clientWidth, stage.clientHeight);
  if (!S.cur || S.cur.media_status !== 'ready') return;
  const R = contentRect(), f = S.frame;
  if (S.showDrawings) {
    const N = S.onion ? S.onionN : 0;
    for (let d = N; d >= 1; d--) {
      const a = 0.55 * (1 - (d - 1) / (N + 1));
      for (const [g, col] of [[f - d, ONION[0]], [f + d, ONION[1]]]) for (const s of shapesStartingAt(g)) drawShape(ctx, { ...s, c: col }, R, a);
    }
    for (const n of visibleNotes(f)) {
      const dim = S.selected && S.selected !== n.id && n.frame !== f ? 0.6 : 1;
      for (const s of n.shapes) drawShape(ctx, s, R, dim);
    }
    for (const s of heldDraft(f) || []) drawShape(ctx, s, R);
  }
  for (const d of Object.values(S.remoteDrafts)) if (d.version_id === S.cur.version_id && d.frame === f) for (const s of d.shapes) drawShape(ctx, s, R, 0.85);
  const fd = frameDraft(f);
  for (const s of fd) drawShape(ctx, s, R);
  if (S.active) drawShape(ctx, S.active, R);
  const sel = S.pick != null && fd[S.pick];
  if (sel) {
    ctx.save();
    if (sel.t !== 'line' && sel.t !== 'arrow') {
      const b = shapeBox(ctx, sel, R);
      ctx.setLineDash([4, 3]); ctx.strokeStyle = 'rgba(255,255,255,.8)'; ctx.lineWidth = 1;
      ctx.strokeRect(b.x, b.y, b.w, b.h); ctx.setLineDash([]);
    }
    for (const [, x, y] of shapeHandles(ctx, sel, R)) {
      ctx.beginPath(); ctx.arc(x, y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = '#fff'; ctx.fill(); ctx.lineWidth = 1.5; ctx.strokeStyle = '#0796d4'; ctx.stroke();
    }
    ctx.restore();
  }
  if (S.tool === 'eraser' && S.hover) {
    ctx.save(); ctx.beginPath(); ctx.arc(S.hover[0], S.hover[1], eraserRadius(R), 0, Math.PI * 2);
    ctx.strokeStyle = 'rgba(255,255,255,.9)'; ctx.lineWidth = 1; ctx.stroke(); ctx.restore();
  }
}

// ─── pointer: draw, select / move / resize, pan ─────────────────────────────
function toNorm(e) {
  const r = canvas.getBoundingClientRect(), R = contentRect();
  return [clamp((e.clientX - r.left - R.x) / R.w, -0.2, 1.2), clamp((e.clientY - r.top - R.y) / R.h, -0.2, 1.2)];
}
const round4 = v => Math.round(v * 10000) / 10000;
const local2 = e => { const r = canvas.getBoundingClientRect(); return [e.clientX - r.left, e.clientY - r.top]; };
const pressure = (ev, brush) => (brush ? [round4(ev.pointerType === 'pen' ? ev.pressure : 0.5)] : []);
let drag = null;

function beginDrawing() {
  if (S.playing) { video.pause(); S.frame = timeToFrame(video.currentTime); }
  local();
}
// What is under the pointer on the picked shape: a resize handle, its body, or nothing
function pickedTarget(e) {
  const s = S.pick != null && frameDraft()[S.pick];
  if (!s) return null;
  const R = contentRect(), [x, y] = local2(e);
  const h = shapeHandles(ctx, s, R).find(([, hx, hy]) => Math.hypot(hx - x, hy - y) < 8);
  if (h) return { k: h[0] };
  return hitShape(ctx, s, toNorm(e), R) ? { k: null } : null;
}
const CURSORS = { nw: 'nwse-resize', se: 'nwse-resize', ne: 'nesw-resize', sw: 'nesw-resize', n: 'ns-resize', s: 'ns-resize', e: 'ew-resize', w: 'ew-resize', a: 'move', b: 'move' };

canvas.addEventListener('pointerdown', e => {
  if (S.cur?.media_status !== 'ready') return;
  if (e.button === 1) { e.preventDefault(); startPan(e, false); return; }
  if (e.button !== 0) return;
  const p = toNorm(e), t = pickedTarget(e);
  if (t) { beginDrawing(); canvas.setPointerCapture(e.pointerId); drag = { kind: t.k ? 'resize' : 'move', k: t.k, p0: p, orig: frameDraft()[S.pick], moved: false }; return; }
  if (S.tool === 'none' || !can('comment')) { selectDown(e, p); return; }
  beginDrawing();
  S.pick = null;
  if (S.tool === 'text') { openTextInput(p); return; }
  canvas.setPointerCapture(e.pointerId);
  if (S.tool === 'eraser') { checkpoint(); S.erasing = { changed: false }; eraseAt(p); return; }
  const base = { t: S.tool === 'brush' ? 'pen' : S.tool, c: S.color, w: S.size, ...opacityProp() };
  if (base.t === 'pen') S.active = { ...base, brush: S.tool === 'brush', pts: [[round4(p[0]), round4(p[1]), ...pressure(e, S.tool === 'brush')]] };
  else S.active = { ...base, a: p.map(round4), b: p.map(round4) };
  renderOverlay();
});

function selectDown(e, p) {
  const R = contentRect();
  let i = frameDraft().findLastIndex(s => hitShape(ctx, s, p, R));
  if (i < 0 && S.showDrawings) {  // a posted drawing of mine → open it for editing
    const n = [...visibleNotes(S.frame)].reverse().find(x => isMine(x) && !x.local && x.shapes.some(s => hitShape(ctx, s, p, R)));
    if (n) { startEdit(n, false); i = frameDraft().findLastIndex(s => hitShape(ctx, s, p, R)); }
  }
  canvas.setPointerCapture(e.pointerId);
  if (i >= 0) {
    beginDrawing();
    S.pick = i;
    drag = { kind: 'move', p0: p, orig: frameDraft()[i], moved: false };
    renderOverlay();
    return;
  }
  const had = S.pick != null;
  S.pick = null;
  startPan(e, !had);
  renderOverlay();
}
function startPan(e, click) { canvas.setPointerCapture(e.pointerId); drag = { kind: 'pan', x0: e.clientX, y0: e.clientY, v0: { ...S.view }, moved: false, click }; }

canvas.addEventListener('pointermove', e => {
  if (drag?.kind === 'move' || drag?.kind === 'resize') {
    const p = toNorm(e);
    if (!drag.moved) { checkpoint(); drag.moved = true; }
    frameDraft()[S.pick] = drag.kind === 'move' ? moveShape(drag.orig, p[0] - drag.p0[0], p[1] - drag.p0[1])
      : resizeShape(ctx, drag.orig, contentRect(), drag.k, p, e.shiftKey);
    renderOverlay();
    return;
  }
  if (drag?.kind === 'pan') {
    const dx = e.clientX - drag.x0, dy = e.clientY - drag.y0;
    if (Math.hypot(dx, dy) > 3) drag.moved = true;
    if (drag.moved && S.view.z > 1) { S.view = { ...drag.v0, x: drag.v0.x + dx, y: drag.v0.y + dy }; layout(); }
    return;
  }
  if (S.tool === 'eraser') { S.hover = local2(e); if (S.erasing) eraseAt(toNorm(e)); else renderOverlay(); return; }
  if (!S.active) {
    const t = pickedTarget(e);
    canvas.style.cursor = t ? (t.k ? CURSORS[t.k] : 'move') : '';
    return;
  }
  if (S.active.t === 'pen') {
    const R = contentRect(), evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    for (const ev of evs.length ? evs : [e]) {
      const p = toNorm(ev), last = S.active.pts[S.active.pts.length - 1];
      if (Math.hypot((p[0] - last[0]) * R.w, (p[1] - last[1]) * R.h) < 1.5) continue;
      S.active.pts.push([round4(p[0]), round4(p[1]), ...pressure(ev, S.active.brush)]);
    }
  } else {
    let p = toNorm(e);
    if (e.shiftKey && (S.active.t === 'rect' || S.active.t === 'ellipse')) {  // square / circle
      const R = contentRect(), dx = (p[0] - S.active.a[0]) * R.w, dy = (p[1] - S.active.a[1]) * R.h, m = Math.max(Math.abs(dx), Math.abs(dy));
      p = [S.active.a[0] + Math.sign(dx) * m / R.w, S.active.a[1] + Math.sign(dy) * m / R.h];
    }
    S.active.b = p.map(round4);
  }
  renderOverlay();
});
canvas.addEventListener('pointerleave', () => { if (S.hover) { S.hover = null; renderOverlay(); } });

function endPointer() {
  const d = drag;
  drag = null;
  if (S.erasing) { if (!S.erasing.changed) hist().u.pop(); S.erasing = null; return; }
  if (d?.kind === 'move' || d?.kind === 'resize') { if (d.moved) afterDraftChange(); return; }
  if (d?.kind === 'pan') { if (!d.moved && d.click) { local(); togglePlay(); } return; }
  const s = S.active;
  S.active = null;
  if (!s) return;
  delete s.brush;
  const R = contentRect();
  if (s.t === 'pen') pushShape(s);
  else if (Math.hypot((s.b[0] - s.a[0]) * R.w, (s.b[1] - s.a[1]) * R.h) > 4) { pushShape(s); S.pick = frameDraft().length - 1; renderOverlay(); }
  else renderOverlay();
}
canvas.addEventListener('pointerup', endPointer);
canvas.addEventListener('pointercancel', endPointer);
canvas.addEventListener('dblclick', e => {
  const p = toNorm(e), R = contentRect(), i = frameDraft().findLastIndex(s => s.t === 'text' && hitShape(ctx, s, p, R));
  if (i >= 0) { S.pick = i; openTextInput(frameDraft()[i].a, i); }
});

// ─── drawing history: undo / redo snapshots per frame (or per note being edited) ──
const hist = () => (S.hist[S.editing ? `e${S.editing.id}` : `${S.cur?.version_id}:${S.frame}`] ||= { u: [], r: [] });
function checkpoint() {
  const h = hist();
  h.u.push(clone(frameDraft()));
  if (h.u.length > 100) h.u.shift();
  h.r = [];
}
function travel(from, to) {
  const h = hist();
  if (!h[from].length) return;
  h[to].push(clone(frameDraft()));
  layer().splice(0, Infinity, ...h[from].pop());
  S.pick = null;
  afterDraftChange();
}
const undo = () => travel('u', 'r'), redo = () => travel('r', 'u');

function pushShape(shape) {
  checkpoint();
  layer().push(shape);
  afterDraftChange();
}
function clearDraft() {
  if (!frameDraft().length) return;
  checkpoint();
  frameDraft().splice(0);
  S.pick = null;
  afterDraftChange();
}
function deletePicked() {
  if (S.pick == null) return;
  checkpoint();
  frameDraft().splice(S.pick, 1);
  S.pick = null;
  afterDraftChange();
}
function editPicked(fn) {  // apply a change to the picked shape, undoably
  const arr = frameDraft(), s = S.pick != null && arr[S.pick];
  if (!s) return false;
  checkpoint();
  arr[S.pick] = fn(s);
  afterDraftChange();
  return true;
}
// Cut, copy and paste between frames, versions and tabs: the selected drawing, else all of this frame's
function clipSource() {
  const fd = frameDraft(), p = pickedShape();
  if (p) return { shapes: [p], picked: true };
  if (fd.length) return { shapes: fd, frame: S.frame, hold: S.editing ? undefined : draftHold(S.frame) };
  const held = heldDraft(S.frame);
  if (held) { const f = draftFrames().find(k => S.store.drafts[S.cur.version_id][k] === held); return { shapes: held, frame: f, hold: draftHold(f) }; }
  const posted = visibleNotes(S.frame).flatMap(n => n.shapes);
  return posted.length ? { shapes: posted, posted: true } : null;
}
const clipRead = () => { try { return JSON.parse(localStorage.rt_clip || 'null'); } catch (e) { return null; } };
function clipCopy() {
  const src = S.cur && clipSource();
  if (!src) return false;
  localStorage.rt_clip = JSON.stringify({ shapes: src.shapes, hold: src.hold });
  renderToolbar();
  return true;
}
function clipCut() {
  if (!can('comment') || !S.cur) return;
  const src = clipSource();
  if (!src) return;
  if (src.posted) return toast('Posted notes can only be copied', true);
  clipCopy();
  beginDrawing();
  checkpoint();
  if (src.picked) { frameDraft().splice(S.pick, 1); S.pick = null; }
  else if (src.frame === S.frame) layer().splice(0, Infinity);
  else { delete drafts()[src.frame]; delete S.hist[`${S.cur.version_id}:${src.frame}`]; }
  S.draftSel = null;
  afterDraftChange();
}
function clipPaste() {
  const clip = clipRead();
  if (!can('comment') || !S.cur || !clip?.shapes?.length) return;
  beginDrawing();
  checkpoint();
  const arr = layer(), wasEmpty = !arr.length;
  arr.push(...clone(clip.shapes));
  S.pick = clip.shapes.length === 1 ? arr.length - 1 : null;
  if (wasEmpty && clip.hold != null && !S.editing) setDraftHold(S.cur.version_id, S.frame, clip.hold);
  afterDraftChange();
}
function clipMenu(x, y) {
  if (!can('comment') || !S.cur) return;
  const src = clipSource(), mod = /Mac/.test(navigator.platform) ? '⌘' : 'Ctrl+', entries = [];
  const row = (l, k) => `${l} <span class="muted">${mod}${k}</span>`;
  if (src && !src.posted) entries.push({ html: row('Cut', 'X'), run: clipCut });
  if (src) entries.push({ html: row('Copy', 'C'), run: clipCopy });
  if (clipRead()?.shapes?.length) entries.push({ html: row('Paste', 'V'), run: clipPaste });
  if (entries.length) openMenu(x, y, entries, { below: true });
}
canvas.addEventListener('contextmenu', e => {
  e.preventDefault();
  if (!S.cur || S.cur.media_status !== 'ready') return;
  const p = toNorm(e), fd = frameDraft();
  for (let i = fd.length - 1; i >= 0; i--) if (hitShape(ctx, fd[i], p, contentRect())) { S.pick = i; renderOverlay(); break; }
  clipMenu(e.clientX, e.clientY);
});
function afterDraftChange() {
  saveStore();
  renderOverlay(); renderTimeline(); renderComposer();
  broadcastDraft();
}
// Freehand strokes lose only the part under the eraser; other shapes go whole
function eraseAt(p) {
  const arr = frameDraft(), R = contentRect(), r = eraserRadius(R);
  const next = arr.flatMap(s => (s.t === 'pen' ? eraseStroke(s, p, r, R) : hitShape(ctx, s, p, R) ? [] : [s]));
  if (next.length !== arr.length || next.some((s, i) => s !== arr[i])) {
    arr.splice(0, Infinity, ...next);
    S.erasing.changed = true;
    S.pick = null;
    afterDraftChange();
  } else renderOverlay();
}

function openTextInput(p, editIndex = null) {
  const ti = $('#textInput'), R = contentRect(), s = editIndex != null ? frameDraft()[editIndex] : null;
  const size = s ? s.s : Math.max(0.025, S.size * 6);
  ti.hidden = false;
  ti.value = s ? s.txt : '';
  ti.style.left = `${R.x + p[0] * R.w}px`;
  ti.style.top = `${R.y + p[1] * R.h}px`;
  ti.style.color = s ? s.c : S.color;
  ti.style.fontSize = `${size * R.h}px`;
  ti.dataset.pos = JSON.stringify(p.map(round4));
  ti.dataset.size = size;
  ti.dataset.edit = editIndex ?? '';
  setTimeout(() => { ti.focus(); ti.select(); }, 0);
}
function commitText() {
  const ti = $('#textInput');
  if (ti.hidden) return;
  ti.hidden = true;
  const txt = ti.value.replace(/\s+$/, ''), i = ti.dataset.edit === '' ? null : +ti.dataset.edit;
  if (i != null) {
    checkpoint();
    const arr = frameDraft();
    if (txt) arr[i] = { ...arr[i], txt }; else arr.splice(i, 1);
    S.pick = txt ? i : null;
    afterDraftChange();
  } else if (txt) { pushShape({ t: 'text', c: S.color, s: +ti.dataset.size, a: JSON.parse(ti.dataset.pos), txt, ...opacityProp() }); S.pick = frameDraft().length - 1; renderOverlay(); }
}
$('#textInput').addEventListener('keydown', e => {
  e.stopPropagation();
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); commitText(); }
  if (e.key === 'Escape') $('#textInput').hidden = true;
});
$('#textInput').addEventListener('input', e => { e.target.style.height = 'auto'; e.target.style.height = `${e.target.scrollHeight}px`; });
$('#textInput').addEventListener('blur', commitText);

// ─── timeline + compositor ──────────────────────────────────────────────────
// Both share one window over the sequence: S.tl = {z: zoom, x: first visible sequence frame}
const shortName = s => (s.entity_name || s.version_code || '').replace(/^.*?_(sq\d+_s\d+)$/i, '$1');
const tlSpan = () => S.seq.total / S.tl.z;
const tlPos = g => `${(g - S.tl.x) / tlSpan() * 100}%`;
const tlLen = n => `${n / tlSpan() * 100}%`;
function clampTl() {
  S.tl.z = clamp(S.tl.z, 1, Math.max(1, S.seq.total / 12));
  S.tl.x = clamp(S.tl.x, 0, S.seq.total - tlSpan());
}
let tlFrame = 0;
function renderTl() {  // zoom / pan: at most once per animation frame
  if (!tlFrame) tlFrame = requestAnimationFrame(() => { tlFrame = 0; renderTimeline(); renderComp(); });
}
// Keep the playhead in view while zoomed in
function followPlayhead() {
  if (S.tl.z <= 1) return;
  const g = gFrame(), V = tlSpan();
  if (g >= S.tl.x && g < S.tl.x + V) return;
  S.tl.x = g - V * 0.1;
  clampTl(); renderTimeline(); renderComp();
}
// Lane per note range so overlapping ranges stay readable
function laneOf(spans) {
  const ends = [];
  return spans.sort((p, q) => p.a - q.a).map(sp => {
    let l = ends.findIndex(e => e < sp.a);
    if (l < 0) l = Math.min(ends.length, 2);
    ends[l] = Math.max(ends[l] ?? -1, sp.b);
    return { ...sp, lane: l };
  });
}

function renderTimeline() {
  const q = S.seq, x0 = S.tl.x, x1 = x0 + tlSpan(), pxPerFrame = ($('#track').clientWidth || 1) / tlSpan();
  const vis = (a, b) => b >= x0 && a <= x1;
  let segs = '', m = '', spans = [];
  if (q.segs.length > 1) {
    q.segs.forEach((s, i) => {
      if (!vis(s.offset, s.offset + s.len)) return;
      const off = q.shots && S.cutFilter && !cutMatches(q.shots[i], S.cutFilter).some(v => v.version_id === s.version_id);
      segs += `<div class="seg${i === q.i ? ' cur' : ''}${s.proxy_url ? '' : ' wait'}${off ? ' off' : ''}" style="left:${tlPos(s.offset)};width:${tlLen(s.len)};--c:${statusOf(s.sg_status).color}">
        ${s.len * pxPerFrame > 44 ? `<span>${esc(shortName(s))}</span>` : ''}</div>`;
    });
  }
  for (const s of q.segs) {
    if (!vis(s.offset, s.offset + s.len)) continue;
    for (const n of topNotes(s)) {
      const col = authorColor(n.author_name), end = noteEnd(n, s), g = s.offset + n.frame;
      if (vis(g, g)) m += `<div class="mk${n.shapes?.length ? ' draw' : ''}${n.id === S.selected ? ' sel' : ''}${n.resolved ? ' resolved' : ''}${n.local ? ' draft' : ''}"
             data-id="${n.id}" data-vid="${s.version_id}" style="left:${tlPos(g + 0.5)};background:${col}"
             title="${noteLabel(n, s)} · ${esc(n.author_name)}: ${esc((n.text || '').slice(0, 80))}"></div>`;
      if (end > n.frame) spans.push({ a: g, b: s.offset + end, col, sel: n.id === S.selected, done: n.resolved });
    }
    for (const f of Object.keys(S.store.drafts[s.version_id] || {}).map(Number)) {
      const h = draftHold(f, s.version_id), sel = S.draftSel?.vid === s.version_id && S.draftSel.f === f;
      const end = h > 0 ? Math.min(f + h, s.len - 1) : f;
      if (vis(s.offset + f, s.offset + f)) m += `<div class="mk draw draft${sel ? ' sel' : ''}" data-vid="${s.version_id}" data-frame="${f}" style="left:${tlPos(s.offset + f + 0.5)}" title="${rangeLabel(f, end, s)}"></div>`;
      if (end > f) spans.push({ a: s.offset + f, b: s.offset + end, draft: true, sel });
    }
  }
  if (S.cur) for (const [k, next] of draftHolds()) spans.push({ a: S.cur.offset + k, b: S.cur.offset + Math.min(next, S.cur.len) - 1, draft: true });
  $('#segs').innerHTML = segs;
  $('#markers').innerHTML = m;
  $('#ranges').innerHTML = laneOf(spans.filter(sp => sp.b > sp.a && vis(sp.a, sp.b))).map(sp =>
    `<div class="range${sp.sel ? ' sel' : ''}${sp.done ? ' resolved' : ''}${sp.draft ? ' draft' : ''}" style="left:${tlPos(sp.a)};width:${tlLen(sp.b - sp.a + 1)};--c:${sp.col || 'var(--warn)'};--l:${sp.lane}"></div>`).join('');
  const io = $('#io');
  io.hidden = !S.io;
  $('#btnClearIO').disabled = !S.io;
  if (S.io) Object.assign(io.style, { left: tlPos(S.io.a), width: tlLen(S.io.b - S.io.a + 1) });
  $('#playhead').style.left = tlPos(gFrame() + 0.5);
  const bar = $('#tlbar');
  bar.hidden = S.tl.z <= 1;
  Object.assign($('#tlthumb').style, { left: `${x0 / q.total * 100}%`, width: `${100 / S.tl.z}%` });
}
new ResizeObserver(() => renderTimeline()).observe($('#track'));

(function timelineEvents() {
  const track = $('#track'), tip = $('#hoverTip');
  const gAt = e => { const r = track.getBoundingClientRect(); return clamp(Math.floor(S.tl.x + (e.clientX - r.left) / r.width * tlSpan()), 0, S.seq.total - 1); };
  let mode = null;
  track.addEventListener('pointerdown', e => {
    if (!S.cur) return;
    local();
    const marker = e.target.closest('.mk'), handle = e.target.closest('#io i');
    if (S.playing) video.pause();
    if (marker) {
      const i = S.seq.segs.findIndex(s => s.version_id === +marker.dataset.vid);
      if (marker.dataset.id) {
        const n = topNotes(S.seq.segs[i]).find(x => String(x.id) === marker.dataset.id);
        goSeg(i, n.frame, { note: n.id });
        $(`.note[data-id="${n.id}"]`)?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        S.draftSel = null; S.rangeNote = n.id;
      } else { S.rangeNote = null; S.draftSel = { vid: +marker.dataset.vid, f: +marker.dataset.frame }; S.selected = null; goSeg(i, +marker.dataset.frame); renderTimeline(); }
      return;
    }
    track.setPointerCapture(e.pointerId);
    if (handle) { mode = { kind: 'io', a: handle.dataset.h === 'a' ? S.io.b : S.io.a }; return; }  // drag In or Out; the other end stays
    const g = gAt(e), rt = e.shiftKey && rangeTarget();
    if (rt) { mode = { kind: 'range', ...rt }; applyRange(mode, g); return; }  // shift-click: stretch the selected drawing / note
    if (e.shiftKey) { mode = { kind: 'io', a: g }; setIO(g, g); return; }  // shift-drag: In/Out
    S.rangeNote = null;
    if (S.draftSel) { S.draftSel = null; renderTimeline(); }
    mode = { kind: 'scrub', wasPlaying: S.playing };
    const { i, f } = locate(g);
    goSeg(i, f);
  });
  track.addEventListener('pointermove', e => {
    if (!S.cur) return;
    const g = gAt(e), { i, f } = locate(g), s = S.seq.segs[i], r = track.getBoundingClientRect();
    tip.hidden = false;
    tip.style.left = `${e.clientX - r.left}px`;
    tip.textContent = `${S.seq.segs.length > 1 ? `${shortName(s)} ` : ''}${fmtF(f, s)}`;
    if (mode?.kind === 'scrub') goSeg(i, f);
    if (mode?.kind === 'io') { setIO(Math.min(mode.a, g), Math.max(mode.a, g)); goSeg(i, f); }
    if (mode?.kind === 'range') applyRange(mode, g);
  });
  track.addEventListener('pointerleave', () => { tip.hidden = true; });
  track.addEventListener('contextmenu', e => {
    const m = e.target.closest('.mk.draft');
    if (!m) return;
    e.preventDefault();
    const i = S.seq.segs.findIndex(s => s.version_id === +m.dataset.vid);
    S.pick = null;
    if (i >= 0) { goSeg(i, +m.dataset.frame); clipMenu(e.clientX, e.clientY); }
  });
  track.addEventListener('pointerup', () => {
    if (mode?.kind === 'scrub' && mode.wasPlaying) playNow();
    if (mode?.kind === 'range') endRange(mode);
    mode = null;
  });

  // wheel zooms around the pointer; horizontal / shift-wheel pans
  $('#timeline').addEventListener('wheel', e => {
    if (!S.cur) return;
    e.preventDefault();
    const r = track.getBoundingClientRect(), dx = e.deltaX || (e.shiftKey ? e.deltaY : 0);
    if (dx) S.tl.x += dx / r.width * tlSpan();
    else {
      const at = (e.clientX - r.left) / r.width, g = S.tl.x + at * tlSpan();
      S.tl.z *= Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.002));
      clampTl();
      S.tl.x = g - at * tlSpan();
    }
    clampTl(); renderTl();
  }, { passive: false });
  const bar = $('#tlbar');
  bar.addEventListener('pointerdown', e => {
    bar.setPointerCapture(e.pointerId);
    const r = bar.getBoundingClientRect(), x0 = S.tl.x, cx = e.clientX;
    if (!e.target.closest('#tlthumb')) { S.tl.x = (e.clientX - r.left) / r.width * S.seq.total - tlSpan() / 2; clampTl(); renderTl(); }
    const from = S.tl.x, move = ev => { S.tl.x = from + (ev.clientX - cx) / r.width * S.seq.total; clampTl(); renderTl(); };
    bar.addEventListener('pointermove', move);
    bar.addEventListener('pointerup', () => bar.removeEventListener('pointermove', move), { once: true });
  });
  bar.addEventListener('dblclick', () => { S.tl = { z: 1, x: 0 }; renderTl(); });
})();

// Shift-click on the timeline stretches the unposted drawing, or your posted note, whose marker was just clicked
function rangeTarget() {
  const d = S.draftSel, i = d ? S.seq.segs.findIndex(s => s.version_id === d.vid) : -1;
  if (d && i >= 0 && S.store.drafts[d.vid]?.[d.f]?.length) {
    const h = Math.max(0, draftHold(d.f, d.vid) ?? 0);
    return { draft: true, vid: d.vid, i, start: d.f, end: d.f + h, f: d.f };
  }
  const n = S.rangeNote && topNotes().find(x => x.id === S.rangeNote);
  if (!n || !isMine(n) || n.local) return null;
  return { note: n, i: S.seq.i, start: n.frame, end: noteEnd(n), f: n.frame, hold: n.hold };
}
function applyRange(m, g) {
  const s = S.seq.segs[m.i], x = clamp(g - s.offset, 0, s.len - 1);
  const [a, b] = x >= m.start ? [m.start, x] : [x, m.end];  // past the start: extend the end; before it: extend the start
  if (m.draft) {
    const fr = S.store.drafts[m.vid];
    if (a !== m.f && fr[a]?.length) return;  // another drawing already starts there
    if (a !== m.f) { fr[a] = fr[m.f]; delete fr[m.f]; delete S.store.holds?.[m.vid]?.[m.f]; delete S.hist[`${m.vid}:${m.f}`]; m.f = a; S.draftSel = { vid: m.vid, f: a }; }
    setDraftHold(m.vid, a, b - a);
  } else { m.note.frame = a; m.note.hold = b - a; }
  goSeg(m.i, x);
  renderTimeline(); renderOverlay(); renderComposer();
}
async function endRange(m) {
  if (m.draft) { saveStore(); return; }
  try { upsertNote(await api(`/notes/${m.note.id}`, { method: 'PATCH', body: { frame: m.note.frame, hold: m.note.hold } })); }
  catch (e) { toast(e.message, true); m.note.frame = m.f; m.note.hold = m.hold; }
  renderNotes(); renderTimeline(); renderOverlay();
}

// In/Out in sequence frames: playback stays inside it; a note covers it only when "In → Out" is picked
function setIO(a, b) {
  S.io = a == null ? null : { a, b };
  if (!S.io) S.noteIO = false;
  applyAudio(); preloadNext();
  renderTimeline(); renderComposer(); renderOverlay();
}
// In/Out clipped to the current segment, in its local frames
function ioLocal() {
  if (!S.io || !S.cur) return null;
  const a = Math.max(S.io.a - S.cur.offset, 0), b = Math.min(S.io.b - S.cur.offset, S.cur.len - 1);
  return a <= b ? { a, b } : null;
}

// Options count the shots each choice would match given the other filter. Redrawn only on change: an open dropdown must survive playback.
let cutFilterDrawn = '';
function renderCutFilter() {
  const el = $('#cutFilter'), q = S.seq, cut = q.ep && S.cuts[q.ep];
  if (!q.shots || !cut) { el.hidden = true; cutFilterDrawn = ''; return; }
  const f = S.cutFilter || { task: '', status: '', only: false };
  const base = q.kind === 'cut' ? cut.shots : q.shots;
  const count = g => base.filter(s => cutMatches(s, g).length).length;
  const codes = [...new Set(base.flatMap(s => s.versions.map(v => v.sg_status)))];
  const taskOpts = [...cut.tasks].reverse().map(t => ({ v: t.name, l: t.name, n: count({ task: t.name, status: f.status }) }));
  const statusOpts = S.statuses.filter(s => codes.includes(s.code)).map(s => ({ v: s.code, l: s.name, n: count({ task: f.task, status: s.code }) }));
  const options = (all, opts, cur) => `<option value="">${all}</option>${opts.map(o =>
    `<option value="${esc(o.v)}"${o.v === cur ? ' selected' : ''}${o.n || o.v === cur ? '' : ' disabled'}>${esc(o.l)} (${o.n})</option>`).join('')}`;
  const html = `<span class="muted">Load</span>
    <button id="cfLatest" class="${S.cutFilter && !f.task && !f.status ? 'active' : ''}" title="Play the latest version of every shot">Latest</button>
    <select id="cfTask" title="Only this task">${options('Any task', taskOpts, f.task)}</select>
    <select id="cfStatus" title="Only this status">${options('Any status', statusOpts, f.status)}</select>
    ${q.kind === 'cut' ? `<label title="Leave the other shots out of the timeline"><input type="checkbox" id="cfOnly"${f.only ? ' checked' : ''}> Only matching shots</label>` : ''}
    ${S.cutFilter ? `<span class="muted">${count(f)} of ${base.length} shots match</span>` : ''}`;
  el.hidden = false;
  if (html !== cutFilterDrawn) { el.innerHTML = html; cutFilterDrawn = html; }
}
$('#cutFilter').addEventListener('change', e => {
  const f = { task: $('#cfTask').value, status: $('#cfStatus').value, only: !!$('#cfOnly')?.checked };
  e.target.blur();  // so the player's shortcuts work again straight away
  applyCutFilter(f);
});
$('#cutFilter').addEventListener('click', e => {
  if (e.target.closest('#cfLatest')) applyCutFilter({ task: '', status: '', only: !!S.cutFilter?.only });
});

function renderComp() {
  renderCutFilter();
  const el = $('#comp'), q = S.seq, cut = q.ep && S.cuts[q.ep];
  if (!q.shots || !cut) { el.hidden = true; el.innerHTML = ''; return; }
  const present = new Set(q.shots.flatMap(s => s.versions.map(v => v.task_name)));
  const tasks = cut.tasks.filter(t => present.has(t.name)).reverse();  // latest step on top
  el.hidden = false;
  const x0 = S.tl.x, x1 = x0 + tlSpan();
  el.innerHTML = tasks.map(t => `<div class="row"><span class="lbl" data-task="${esc(t.name)}">${esc(t.name)}</span>${
    q.segs.map((s, i) => {
      const v = q.shots[i].versions.find(x => x.task_name === t.name);
      if (!v || s.offset > x1 || s.offset + s.len < x0) return '';
      const st = statusOf(v.sg_status);
      return `<div class="cell${s.task_name === t.name ? ' on' : ''}" data-i="${i}" data-task="${esc(t.name)}"
        style="left:${tlPos(s.offset)};width:${tlLen(s.len)};--c:${st.color}" title="${esc(v.version_code)} · ${esc(st.name)}"></div>`;
    }).join('')}</div>`).join('');
}
$('#comp').addEventListener('click', e => {
  const q = S.seq;
  const lbl = e.target.closest('.lbl');
  if (lbl) { applyCutFilter({ status: '', only: false, ...S.cutFilter, task: lbl.dataset.task }); return; }
  const cell = e.target.closest('.cell');
  if (!cell) return;
  const i = +cell.dataset.i, task = cell.dataset.task, vs = q.shots[i].versions.filter(v => v.task_name === task);
  if (q.segs[i].task_name === task) {  // already showing this step: pick another take
    openMenu(e.clientX, e.clientY, vs.map(v => ({ html: `<i class="dot" style="background:${statusOf(v.sg_status).color}"></i>${esc(v.version_code)}<span class="muted">${new Date(v.created_at * 1000).toLocaleDateString()}</span>`,
      on: v.version_id === q.vids[i], run: () => setSegVersions([[i, v.version_id]]) })));
    return;
  }
  setSegVersions([[i, vs[0].version_id]]);
});

function openMenu(x, y, entries, { below = false, narrow = false } = {}) {
  const m = $('#menu');
  m.classList.toggle('narrow', narrow);
  m.innerHTML = entries.map((en, k) => `<div class="mi${en.on ? ' on' : ''}" data-k="${k}">${en.html}</div>`).join('');
  m.hidden = false;
  const r = m.getBoundingClientRect();
  m.style.left = `${Math.min(x, innerWidth - r.width - 8)}px`;
  m.style.top = `${below ? clamp(y, 8, Math.max(8, innerHeight - r.height - 8)) : Math.max(8, y - r.height - 8)}px`;
  m.onclick = e => { const it = e.target.closest('.mi'); if (it) { m.hidden = true; entries[+it.dataset.k].run(); } };
}
document.addEventListener('pointerdown', e => { if (!e.target.closest('#menu')) $('#menu').hidden = true; });

// ─── sorting the list: this person's view, the playlist keeps its own order ─
const SORT_STORE = `rt_sort_${TOKEN}`;
try { S.sort = JSON.parse(localStorage[SORT_STORE] || 'null'); } catch (e) { S.sort = null; }
S.sortVals = {}; S.sortFields = {};
// Known from the items already; anything else is read from ShotGrid through /sort
const SORT_LOCAL = {
  version_code: it => it.version_code, entity_name: it => it.entity_name, task_name: it => it.task_name, artist_name: it => it.artist_name,
  sg_status: it => { const i = S.statuses.findIndex(s => s.code === it.sg_status); return i < 0 ? null : i; },
};
const sortText = v => (Array.isArray(v) ? v.join(', ') : String(v));
function compareSortVals(x, y) {
  if (typeof x !== 'string' && !Array.isArray(x) && typeof y !== 'string' && !Array.isArray(y)) return Number(x) - Number(y);
  return sortText(x).localeCompare(sortText(y), undefined, { numeric: true, sensitivity: 'base' });
}
function sortItems(items) {
  const inPlaylist = (a, b) => a.position - b.position || a.id - b.id, s = S.sort;
  if (!s) return [...items].sort(inPlaylist);
  const local = SORT_LOCAL[s.key], dir = s.dir === 'desc' ? -1 : 1, empty = v => v == null || v === '' || (Array.isArray(v) && !v.length);
  if (!local) requestSortVals(items);
  const val = local || (it => S.sortVals[it.version_id]);
  return [...items].sort((a, b) => {
    const x = val(a), y = val(b), ex = empty(x), ey = empty(y);
    if (ex || ey) return ex === ey ? inPlaylist(a, b) : ex ? 1 : -1;  // blanks last, whichever way it's sorted
    return dir * compareSortVals(x, y) || inPlaylist(a, b);
  });
}
function requestSortVals(items) {
  const key = S.sort.key;
  if (S.sortBusy || items.every(it => it.version_id in S.sortVals)) return;
  S.sortBusy = true;
  api(`/sort?field=${encodeURIComponent(key)}`).then(r => {
    S.sortBusy = false;
    if (S.sort?.key !== key) return applySort();
    for (const it of S.items) if (!(it.version_id in r.vals)) r.vals[it.version_id] = null;
    S.sortVals = r.vals;
    applySort();
  }).catch(e => {
    S.sortBusy = false;
    if (S.sort?.key === key) { toast(e.message, true); S.sort = null; applySort(); }
  });
}
function applySort() {
  if (S.sort) localStorage[SORT_STORE] = JSON.stringify(S.sort); else localStorage.removeItem(SORT_STORE);
  S.items = sortItems(S.items);
  renderSidebar();
}
function setSort(key, name) {
  if (!key) S.sort = null;
  else if (S.sort?.key === key) S.sort.dir = S.sort.dir === 'desc' ? 'asc' : 'desc';
  else { S.sort = { key, name, dir: 'asc' }; S.sortVals = {}; }
  applySort();
}
const sortArrow = () => (S.sort?.dir === 'desc' ? '↓' : '↑');
function renderSortChip() {
  const c = $('#sortChip');
  c.hidden = !S.sort;
  if (S.sort) c.textContent = `${S.sort.name} ${sortArrow()}`;
}
function sortChoices() {
  const f = S.sortFields, full = can('open'), out = [
    { key: 'version_code', name: 'Version' }, { key: 'entity_name', name: 'Shot' },
    full && f.task_order ? { key: f.task_order, name: 'Task' } : { key: 'task_name', name: 'Task' },
    { key: 'sg_status', name: 'Status' }, { key: 'artist_name', name: 'Artist' }];
  if (full) out.push({ key: 'created_at', name: 'Date created' });
  if (full && f.cut_order) out.push({ key: f.cut_order, name: 'Cut order' });
  if (S.sort && !out.some(o => o.key === S.sort.key)) out.push({ key: S.sort.key, name: S.sort.name });
  return out;
}
$('#btnSort').addEventListener('click', e => {
  const r = e.currentTarget.getBoundingClientRect();
  $('#sortPicker').hidden = true;
  openMenu(r.left, r.bottom + 4, [
    { html: S.review?.playlist_id ? 'Playlist order' : 'Order added', on: !S.sort, run: () => setSort(null) },
    ...sortChoices().map(o => ({ html: `${esc(o.name)}${S.sort?.key === o.key ? ` ${sortArrow()}` : ''}`, on: S.sort?.key === o.key, run: () => setSort(o.key, o.name) })),
    ...(can('details') ? [{ html: 'Other field…', run: openSortPicker }] : []),
  ], { below: true });
});
$('#sortChip').addEventListener('click', () => setSort(S.sort.key, S.sort.name));
async function openSortPicker() {
  try { S.fieldDefs ||= await api('/fields'); } catch (e) { return toast(e.message, true); }
  $('#sortPicker').hidden = false;
  $('#sortSearch').value = '';
  renderSortPicker();
  $('#sortSearch').focus();
}
function renderSortPicker() {
  $('#sortFields').innerHTML = fieldTree($('#sortSearch').value.trim().toLowerCase(), new Set(S.sort ? [S.sort.key] : []));
}
$('#sortSearch').addEventListener('input', renderSortPicker);
$('#sortSearch').addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Escape') $('#sortPicker').hidden = true; });
$('#btnSortClose').addEventListener('click', () => { $('#sortPicker').hidden = true; });
$('#sortFields').addEventListener('click', async e => {
  const fx = e.target.closest('.fx');
  if (fx) { await toggleFieldBranch(fx); return renderSortPicker(); }
  const row = e.target.closest('.fopt');
  if (!row?.dataset.k) return;
  $('#sortPicker').hidden = true;
  setSort(row.dataset.k, row.querySelector('.fn').textContent);
});

// ─── sidebar, header, stage message ─────────────────────────────────────────
function renderSidebar() {
  renderSortChip();
  const inSeq = new Set(S.seq.segs.map(s => s.item_id).filter(Boolean));
  $('#itemList').innerHTML = S.items.map(it => {
    const m = S.media[it.version_id] || {}, st = statusOf(it.sg_status);
    const tops = itemNoteRows(it.id).filter(n => !n.parent_id).length, pend = itemNoteRows(it.id).filter(pending).length;
    const viewers = S.presence.filter(p => p.item_id === it.id && p.cid !== S.cid);
    let thumb;
    if (m.status === 'ready') thumb = `<div class="thumb" style="background-image:url('${API}/media/${it.version_id}/thumb.jpg')"></div>`;
    else if (m.status === 'error' || m.status === 'nomedia') thumb = `<div class="thumb" title="${esc(m.error)}">⚠</div>`;
    else thumb = `<div class="thumb"><div class="bar" style="width:${(m.progress || 0) * 100}%"></div></div>`;
    const cls = [S.cur?.item_id === it.id && 'current', S.multi.has(it.id) && 'multi', S.seq.segs.length > 1 && inSeq.has(it.id) && 'inseq'].filter(Boolean).join(' ');
    return `<li data-id="${it.id}" class="${cls}">
      ${thumb}
      <div class="meta">
        <div class="code" title="${esc(it.version_code)}">${midEllipsis(it.version_code)}</div>
        <div class="sub">${esc([it.task_name, it.artist_name].filter(Boolean).join(' · '))}</div>
        <div class="badges">
          <span class="st" style="--c:${st.color}">${esc(st.name)}</span>
          ${tops ? `<span class="cnt" title="${tops} notes">${svg(ICONS.chat)}${tops}</span>` : ''}
          ${pend ? `<span class="cnt pending" title="${pend} unpublished">${pend}</span>` : ''}
        </div>
      </div>
      ${viewers.length ? `<div class="viewers">${viewers.map(v => avatar(v.name, 'sm', v.id)).join('')}</div>` : ''}
    </li>`;
  }).join('');
}
function pickItem(e) {
  const li = e.target.closest('li[data-id]');
  if (!li) return;
  local();
  const it = S.items.find(i => i.id === +li.dataset.id), toggle = e.metaKey || e.ctrlKey;
  if (!toggle && !e.shiftKey) { S.selFrom = it.id; return openMode(S.mode, it.version_id); }
  // Cmd/Ctrl toggles one, Shift selects the run from the last plain or Cmd click; the selection plays back to back
  const from = S.items.find(i => i.id === S.selFrom) || S.items.find(i => i.version_id === S.anchor);
  if (!S.multi.size && from) S.multi.add(from.id);
  if (e.shiftKey && from) {
    const [a, b] = [S.items.indexOf(from), S.items.indexOf(it)].sort((x, y) => x - y);
    S.multi = new Set(S.items.slice(a, b + 1).map(x => x.id));
  } else {
    S.multi.has(it.id) ? S.multi.delete(it.id) : S.multi.add(it.id);
    S.selFrom = it.id;
  }
  const vids = S.items.filter(i => S.multi.has(i.id)).map(i => i.version_id);
  if (vids.length >= 2) setSeq('multi', vids, Math.max(0, vids.indexOf(it.version_id)));
  else if (vids.length === 1) openMode(S.mode, vids[0]);
  renderSidebar();
}
$('#itemList').addEventListener('click', pickItem);
$('#itemList').addEventListener('contextmenu', e => {
  const li = e.target.closest('li[data-id]');
  if (!li) return;
  e.preventDefault();
  if (e.ctrlKey && !e.metaKey) return pickItem(e);  // macOS Ctrl-click
  const it = S.items.find(i => i.id === +li.dataset.id), x = e.clientX, y = e.clientY;
  const its = S.multi.has(it.id) && S.multi.size > 1 ? S.items.filter(i => S.multi.has(i.id)) : [it], n = its.length;
  const label = (one, many) => (n > 1 ? `${many} <span class="muted">${n}</span>` : one);
  const entries = [];
  if (can('status')) entries.push({ html: label('Set status', 'Set status'), run: () => statusMenu(x, y, its.map(i => i.version_id), n === 1 ? it.sg_status : null) });
  if (can('publish')) entries.push({ html: label('Publish to SG', 'Publish to SG'), run: () => openPublish(its.map(i => i.id)) });
  if (can('download')) entries.push({ html: label('Export…', 'Export…'), run: () => openExport(its.map(i => i.id)) });
  entries.push({ html: label('Copy name', 'Copy names'), run: () => navigator.clipboard.writeText(its.map(i => i.version_code).join('\n')).then(() => toast('Copied')) });
  if (n === 1) entries.push({ html: 'Open in ShotGrid', run: () => window.open(`${SG_URL}/detail/Version/${it.version_id}`, '_blank', 'noopener') });
  if (can('add')) entries.push({ html: label('Reprocess', 'Reprocess'), run: () => reprocessItems(its) });
  if (can('add')) entries.push({ html: label('Remove from review', 'Remove from review'), run: () => removeItems(its) });
  openMenu(x, y, entries, { below: true });
});
// Makes the movie again with the current Transcoding settings. Notes and drawings stay.
async function reprocessItems(its) {
  const res = await Promise.allSettled(its.map(i => api(`/items/${i.id}/reingest`, { method: 'POST' })));
  const failed = res.find(r => r.status === 'rejected');
  if (failed) toast(failed.reason.message, true);
}
async function removeItems(its) {
  const free = its.filter(i => !itemNoteRows(i.id).length), kept = its.length - free.length;
  if (!free.length) return toast('Versions with notes stay in the review', true);
  if (!confirm(`Remove ${free.length === 1 ? free[0].version_code : `${free.length} versions`} from this review?${kept ? ` ${kept} with notes will stay.` : ''}`)) return;
  const res = await Promise.allSettled(free.map(i => api(`/items/${i.id}`, { method: 'DELETE' })));
  const failed = res.find(r => r.status === 'rejected');
  if (failed) toast(failed.reason.message, true);
  S.multi.clear();
  await reload();
}

const panelBtn = ([k, ic, name]) => `<button class="ghost${S.hide[k] ? '' : ' on'}" data-panel="${k}" title="${name}">${svg(ICONS[`panel${ic}`])}</button>`;
function renderHeader() {
  const it = S.cur, h = $('#itemHeader');
  if (!it) { h.innerHTML = `${panelBtn(PANELS[0])}<span class="spacer"></span>${PANELS.slice(1).map(panelBtn).join('')}`; return; }
  const st = statusOf(it.sg_status), anchor = S.anchor && segFor(S.anchor);
  const info = [it.entity_name, it.task_name, it.artist_name, it.frame_count && `${dispFrame(0)}–${dispFrame(it.frame_count - 1)}`,
    it.fps && `${it.fps} fps`, it.width && `${it.width}×${it.height}`].filter(Boolean).join(' · ');
  h.innerHTML = `${panelBtn(PANELS[0])}<span class="code" title="${esc(info)}">${midEllipsis(it.version_code)}</span>
    <button id="statusBtn" class="status" style="--c:${st.color}" ${can('status') ? '' : 'disabled'}><span>${esc(st.name)}</span>${can('status') ? '<svg viewBox="0 0 10 10" width="9" height="9"><path d="M1.5 3.5L5 7l3.5-3.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>' : ''}</button>
    ${it.media_status === 'error' || it.media_status === 'nomedia' ? (it.item_id ? '<button id="btnReingest">Retry</button>' : '') : ''}
    <span class="spacer"></span>
    ${(!can('versions') || !S.cutLabel) && S.seq.kind !== 'multi' ? '' : S.seq.kind === 'multi'
      ? `<div class="seg"><button class="active">${S.seq.segs.length} selected</button><button id="btnExitMulti" title="Clear selection">×</button></div>`
      : `<div class="seg" id="modeSeg">${modes().map(([k, l]) => `<button data-mode="${k}" class="${S.mode === k ? 'active' : ''}" ${k !== 'one' && anchor?.entity_type !== 'Shot' ? 'disabled' : ''}>${esc(l)}</button>`).join('')}</div>`}
    ${can('versions') ? `<button id="btnCompare" class="${S.cmp ? 'active' : ''}" ${it.entity_id ? '' : 'disabled'}>Compare</button>` : ''}
    <a class="iconlink" href="${SG_URL}/detail/Version/${it.version_id}" target="_blank" rel="noopener" title="Open in ShotGrid">${svg(ICONS.ext)}</a>
    <span class="panels">${PANELS.slice(1).map(panelBtn).join('')}</span>`;
}
// Collapsible areas: versions, header, drawing tools, notes (\ toggles all)
function applyPanels() {
  for (const [k] of PANELS) document.body.classList.toggle(`hide-${k}`, !!S.hide[k]);
  localStorage.rt_hide = JSON.stringify(S.hide);
  renderHeader();
}
function togglePanel(k) {
  if (k === 'all') { const any = PANELS.some(([p]) => !S.hide[p]); for (const [p] of PANELS) S.hide[p] = any; } else S.hide[k] = !S.hide[k];
  applyPanels();
}
$('#itemHeader').addEventListener('click', async e => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.panel) return togglePanel(b.dataset.panel);
  if (b.dataset.mode) { local(); openMode(b.dataset.mode); }
  if (b.id === 'btnExitMulti') openMode(S.mode);
  if (b.id === 'btnCompare') S.cmp ? closeCompare() : openCompare();
  if (b.id === 'statusBtn' && S.cur) { const r = b.getBoundingClientRect(); statusMenu(r.left, r.bottom + 4, [S.cur.version_id], S.cur.sg_status); }
  if (b.id === 'btnReingest') {
    try { await api(`/items/${S.cur.item_id}/reingest`, { method: 'POST' }); } catch (err) { toast(err.message, true); }
  }
});
function statusMenu(x, y, vids, current) {
  openMenu(x, y, S.statuses.map(s => ({ html: `<span class="sdot" style="--c:${s.color}"></span>${esc(s.name)}`, on: s.code === current, run: () => setStatus(vids, s) })), { below: true });
}
async function setStatus(vids, st) {
  if (vids.length > 1 && !confirm(`Set ${vids.length} versions to ${st.name} in ShotGrid?`)) return;
  const res = await Promise.allSettled(vids.map(v => api(`/versions/${v}/status`, { method: 'POST', body: { status: st.code } })));
  vids.forEach((v, i) => { if (res[i].status === 'fulfilled') applyStatus(v, st.code); });
  const failed = res.filter(r => r.status === 'rejected');
  if (failed.length) toast(failed[0].reason.message, true);
}
function applyStatus(vid, status) {
  if (S.details?.[vid]?.vals && 'sg_status_list' in S.details[vid].vals) { S.details[vid].vals.sg_status_list = status; if (S.rtab === 'details') renderDetails(); }
  for (const it of S.items) if (it.version_id === vid) it.sg_status = status;
  if (S.vinfo[vid]) S.vinfo[vid].sg_status = status;
  for (const cut of Object.values(S.cuts)) for (const sh of cut.shots) for (const v of sh.versions) if (v.version_id === vid) v.sg_status = status;
  rebuildSegs();
  renderHeader(); renderSidebar(); renderTimeline(); renderComp();
}

function renderStageMsg() {
  const m = $('#stageMsg'), it = S.cur;
  if (!it) { m.hidden = false; m.innerHTML = ''; return; }
  if (it.media_status === 'ready') { m.hidden = true; return; }
  m.hidden = false;
  if (it.media_status === 'error') m.innerHTML = `<div>⚠ ${esc(it.media_error || 'Ingest failed')}</div>`;
  else if (it.media_status === 'nomedia') m.innerHTML = '<div>No movie in ShotGrid</div>';
  else m.innerHTML = `<div>${esc(shortName(it))}</div><div class="progress"><div style="width:${(it.media_progress || 0) * 100}%"></div></div>`;
}

// ─── notes panel ────────────────────────────────────────────────────────────
const IMG_NAME = /\.(png|jpe?g|gif|webp)$/i;
function noteFilesHTML(n) {
  if (!n.files?.length) return '';
  return `<div class="nfiles">${n.files.map(f => {
    const url = `${API}/files/${f.id}`;
    if (IMG_NAME.test(f.name)) return `<a class="nimg" href="${url}" target="_blank" rel="noopener" title="${esc(f.name)}"><img loading="lazy" alt="" src="${url}"></a>`;
    const inner = `${svg(ICONS.clip)}<span>${midEllipsis(f.name)}</span>`;
    return can('download') ? `<a class="nfile" href="${url}" download title="${esc(f.name)}">${inner}</a>` : `<span class="nfile" title="${esc(f.name)}">${inner}</span>`;
  }).join('')}</div>`;
}
function noteHTML(n, isReply = false) {
  const sg = n.sg_note_id ? `<a class="sgok" href="${SG_URL}/detail/Note/${n.sg_note_id}" target="_blank" rel="noopener" title="In ShotGrid">${svg(ICONS.check)}</a>`
    : n.sg_reply_id ? `<span class="sgok" title="In ShotGrid">${svg(ICONS.check)}</span>` : '';
  const edited = (n.sg_note_id || n.sg_reply_id) && n.synced_at && n.updated_at > n.synced_at + 1 ? '<span class="sgedit" title="Edited after publishing">edited</span>' : '';
  const actions = [];
  if (!n.local) {
    if (!isReply) actions.push('<a data-act="reply">Reply</a>', `<a data-act="resolve">${n.resolved ? 'Reopen' : 'Resolve'}</a>`);
    if (isMine(n)) actions.push('<a data-act="edit">Edit</a>', '<a data-act="delete">Delete</a>');
    if (!isReply) actions.push(...can('download') ? ['<a data-act="png" title="Annotated frame">PNG</a>'] : [], '<a data-act="link" title="Copy link">Link</a>');
  }
  return `<div class="head">${avatar(n.author_name, 'sm', n.author_id)}<span class="who">${esc(n.author_name)}</span>
      ${!isReply ? `<span class="chip">${noteLabel(n)}</span>` : ''}
      <span class="muted">${n.local ? `<span title="Sending">${svg(ICONS.clock)}</span>` : ago(n.created_at)}</span><span class="spacer"></span>${edited}${sg}</div>
    ${n.text ? `<div class="body">${esc(n.text)}</div>` : ''}
    ${noteFilesHTML(n)}
    ${actions.length ? `<div class="actions" data-id="${n.id}">${actions.join('')}</div>` : ''}`;
}

function renderNotes() {
  renderRightPane();
  const list = $('#noteList');
  if (!S.cur) { list.innerHTML = ''; return; }
  const all = segNotes().sort((a, b) => a.frame - b.frame || a.created_at - b.created_at);
  let tops = all.filter(n => !n.parent_id);
  const f = S.filter;
  if (f === 'open') tops = tops.filter(n => !n.resolved);
  if (f === 'mine') tops = tops.filter(n => isMine(n) || all.some(r => r.parent_id === n.id && isMine(r)));
  if (f === 'pending') tops = tops.filter(n => pending(n) || all.some(r => r.parent_id === n.id && pending(r)));
  list.innerHTML = tops.map(n => {
    const replies = all.filter(r => r.parent_id === n.id).sort((a, b) => a.created_at - b.created_at);
    return `<div class="note${n.id === S.selected ? ' sel' : ''}${S.noteSel.has(n.id) ? ' picked' : ''}${n.resolved ? ' resolved' : ''}${n.local ? ' local' : ''}" data-id="${n.id}" data-frame="${n.frame}" data-end="${noteEnd(n)}">
      ${noteHTML(n)}
      ${replies.length ? `<div class="replies">${replies.map(r => `<div class="reply">${noteHTML(r, true)}</div>`).join('')}</div>` : ''}
      ${S.replyOpen === n.id ? '<div class="reply-box"><textarea rows="1" placeholder="Reply"></textarea><button class="primary" data-act="send-reply">Send</button></div>' : ''}
    </div>`;
  }).join('');
  onFrame();
  if (S.replyOpen) list.querySelector(`.note[data-id="${S.replyOpen}"] .reply-box textarea`)?.focus();
}

$('#noteList').addEventListener('click', async e => {
  const card = e.target.closest('.note');
  if (!card || e.target.closest('textarea, a.sgok, .nfiles')) return;
  const act = e.target.closest('[data-act]')?.dataset.act;
  const notes = segNotes(), byId = id => notes.find(n => String(n.id) === String(id));
  const top = byId(card.dataset.id), target = byId(e.target.closest('.actions')?.dataset.id || card.dataset.id);
  if (!act && !top.local && (e.shiftKey || e.metaKey || e.ctrlKey)) return pickNote(top, e);
  if (S.noteSel.size) S.noteSel.clear();
  S.noteFrom = top.id;
  try {
    if (act === 'reply') { S.replyOpen = S.replyOpen === top.id ? null : top.id; renderNotes(); return; }
    if (act === 'send-reply') { await sendReply(card, top); return; }
    if (act === 'resolve') { upsertNote(await api(`/notes/${top.id}`, { method: 'PATCH', body: { resolved: !top.resolved } })); renderNotes(); renderTimeline(); return; }
    if (act === 'delete') {
      if (!confirm(target.parent_id ? 'Delete this reply?' : 'Delete this note and its replies?')) return;
      await api(`/notes/${target.id}`, { method: 'DELETE' });
      removeNote(target.id);
      return;
    }
    if (act === 'edit') { startEdit(target); return; }
    if (act === 'png') { window.open(`${API}/frame/${top.item_id}/${top.frame}.png?note=${top.id}`, '_blank'); return; }
    if (act === 'link') {
      const url = `${location.origin}/r/${TOKEN}?item=${top.item_id}&note=${top.id}`;
      await navigator.clipboard.writeText(url).then(() => toast('Link copied'), () => prompt('Link', url));
      return;
    }
  } catch (err) { toast(err.message, true); return; }
  local();
  if (S.playing) video.pause();
  S.selected = top.id;
  seek(top.frame);
  renderNotes(); renderTimeline();
});
// Shift selects a run of notes, Cmd/Ctrl toggles one; right-click acts on the selection
function pickNote(top, e) {
  const order = $$('#noteList .note:not(.local)').map(c => +c.dataset.id);
  if (e.shiftKey) {
    const from = order.includes(S.noteFrom) ? S.noteFrom : S.selected && order.includes(S.selected) ? S.selected : top.id;
    const [a, b] = [order.indexOf(from), order.indexOf(top.id)].sort((x, y) => x - y);
    S.noteSel = new Set(order.slice(a, b + 1));
  } else {
    if (!S.noteSel.size && S.selected && order.includes(S.selected)) S.noteSel.add(S.selected);
    S.noteSel.has(top.id) ? S.noteSel.delete(top.id) : S.noteSel.add(top.id);
    S.noteFrom = top.id;
  }
  document.getSelection()?.removeAllRanges();
  renderNotes();
}
$('#noteList').addEventListener('contextmenu', e => {
  const card = e.target.closest('.note:not(.local)');
  if (!card) return;
  e.preventDefault();
  const id = +card.dataset.id;
  if (!S.noteSel.has(id)) { S.noteSel = new Set([id]); S.noteFrom = id; renderNotes(); }
  const notes = S.notes.filter(n => S.noteSel.has(n.id) && !n.deleted), n = notes.length;
  const count = n > 1 ? ` <span class="muted">${n}</span>` : '';
  const patch = async resolved => {
    const res = await Promise.allSettled(notes.filter(x => !!x.resolved !== resolved).map(x => api(`/notes/${x.id}`, { method: 'PATCH', body: { resolved } })));
    res.forEach(r => { if (r.status === 'fulfilled') upsertNote(r.value); else toast(r.reason.message, true); });
    S.noteSel.clear(); renderNotes(); renderTimeline();
  };
  const mine = notes.filter(isMine), entries = [];
  if (can('comment') && notes.some(x => !x.resolved)) entries.push({ html: `Resolve${count}`, run: () => patch(true) });
  if (can('comment') && notes.some(x => x.resolved)) entries.push({ html: `Reopen${count}`, run: () => patch(false) });
  entries.push({ html: `Copy text${count}`, run: () => navigator.clipboard.writeText(notes.map(x => `${noteLabel(x)} ${x.author_name}: ${x.text}`).join('\n')).then(() => toast('Copied')) });
  if (can('comment') && mine.length) entries.push({ html: `Delete${mine.length > 1 ? ` <span class="muted">${mine.length}</span>` : ''}`, run: async () => {
    if (!confirm(mine.length === 1 ? 'Delete this note and its replies?' : `Delete your ${mine.length} notes and their replies?${n > mine.length ? ` ${n - mine.length} from other people will stay.` : ''}`)) return;
    const res = await Promise.allSettled(mine.map(x => api(`/notes/${x.id}`, { method: 'DELETE' })));
    res.forEach((r, i) => { if (r.status === 'fulfilled') removeNote(mine[i].id); else toast(r.reason.message, true); });
    S.noteSel.clear(); renderNotes();
  } });
  openMenu(e.clientX, e.clientY, entries, { below: true });
});
$('#noteList').addEventListener('keydown', e => {
  if (!e.target.matches('.reply-box textarea')) return;
  e.stopPropagation();
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
    const card = e.target.closest('.note');
    sendReply(card, segNotes().find(n => String(n.id) === card.dataset.id)).catch(err => toast(err.message, true));
  }
  if (e.key === 'Escape') { S.replyOpen = null; renderNotes(); }
});
$('#noteFilter').addEventListener('change', e => { S.filter = e.target.value; renderNotes(); });

// ─── right panel: notes on this version, on the whole shot, or its ShotGrid fields ─
const DETAIL_DEFAULT = APP.detail_fields;
function detailKeys() {
  try { return JSON.parse(localStorage.rt_detail_fields) || DETAIL_DEFAULT; } catch (e) { return DETAIL_DEFAULT; }
}
function renderRightPane() {
  S.rtab ||= localStorage.rt_rtab || 'notes';
  if (S.rtab === 'details' && !can('details')) S.rtab = 'notes';
  const tab = S.rtab;
  $$('#rtabs button').forEach(b => b.classList.toggle('active', b.dataset.rtab === tab));
  $('#rtabShot').textContent = S.cur?.entity_type === 'Asset' ? 'Asset notes' : 'Shot notes';
  $('#noteList').hidden = tab !== 'notes';
  $('#shotList').hidden = tab !== 'shot';
  $('#detailsPane').hidden = tab !== 'details';
  $('#noteFilter').hidden = tab !== 'notes';
  $('#btnFields').hidden = tab !== 'details';
  if (tab === 'shot') { renderShotNotes(); loadShotNotes(); }
  if (tab === 'details') { renderDetails(); loadDetails(); }
}
$('#rtabs').addEventListener('click', e => {
  const b = e.target.closest('button[data-rtab]');
  if (b) { S.rtab = localStorage.rt_rtab = b.dataset.rtab; renderRightPane(); }
});

const sameEntity = (a, b) => a.entity_id && a.entity_id === b.entity_id && a.entity_type === b.entity_type;
async function loadShotNotes() {
  const cur = S.cur;
  if (!cur?.entity_id || !can('versions')) return;
  const key = `${cur.entity_type}:${cur.entity_id}`, c = S.sgNotes[key];
  if (c && (c.loading || Date.now() - c.at < 60000)) return;
  S.sgNotes[key] = { ...c, loading: true };
  try {
    const [list] = await Promise.all([api(`/entity_notes?entity_type=${encodeURIComponent(cur.entity_type)}&entity_id=${cur.entity_id}`), entityVersions(cur)]);
    S.sgNotes[key] = { list, at: Date.now() };
  } catch (e) { S.sgNotes[key] = { list: c?.list || [], at: Date.now() }; toast(e.message, true); }
  if (S.rtab === 'shot') renderShotNotes();
}
// This review's notes plus ShotGrid's own, one group per version, newest version first
function renderShotNotes() {
  const el = $('#shotList'), cur = S.cur;
  if (!cur?.entity_id) { el.innerHTML = ''; return; }
  const items = S.items.filter(i => sameEntity(i, cur)), groups = new Map();
  const group = (vid, code) => { if (!groups.has(vid)) groups.set(vid, { vid, code, cards: [] }); return groups.get(vid); };
  const inReview = new Set();
  for (const it of items) for (const n of S.notes.filter(x => x.item_id === it.id && !x.deleted && !x.parent_id).sort((a, b) => a.frame - b.frame)) {
    if (n.sg_note_id) inReview.add(n.sg_note_id);
    const seg = { ...it, fps: S.media[it.version_id]?.fps, frame_count: S.media[it.version_id]?.frame_count };
    const replies = S.notes.filter(x => x.parent_id === n.id && !x.deleted);
    group(it.version_id, it.version_code).cards.push(`<div class="snote" data-vid="${it.version_id}" data-frame="${n.frame}" data-note="${n.id}">
      <div class="head">${avatar(n.author_name, 'sm', n.author_id)}<span class="who">${esc(n.author_name)}</span><span class="chip">${rangeLabel(n.frame, noteEnd(n, seg), seg)}</span><span class="t">${ago(n.created_at)}</span></div>
      <div class="txt">${esc(n.text || '')}</div>${noteFilesHTML(n)}${n.shapes?.length && can('download') && S.media[it.version_id]?.status === 'ready' ? `<img class="sthumb" loading="lazy" alt="" src="${API}/frame/${it.id}/${n.frame}.png?note=${n.id}&u=${Math.round(n.updated_at)}" onerror="this.remove()">` : ''}${replies.map(r => `<div class="rep">${esc(r.author_name)}: ${esc(r.text)}</div>`).join('')}</div>`);
  }
  for (const n of S.sgNotes[`${cur.entity_type}:${cur.entity_id}`]?.list || []) {
    if (inReview.has(n.id)) continue;
    group(n.version_id, n.version_code).cards.push(`<div class="snote" data-sg="${n.id}" ${n.version_id ? `data-vid="${n.version_id}"` : ''}>
      <div class="head">${avatar(n.author_name || '?', 'sm', n.author_id)}<span class="who">${esc(n.author_name)}</span><span class="t">${ago(n.created_at)}</span></div>
      <div class="txt">${esc(n.text)}</div>${n.images.map(id => `<img class="sthumb" loading="lazy" alt="" src="${API}/attachment/${id}" onerror="this.remove()">`).join('')}${n.replies.map(r => `<div class="rep">${esc(r)}</div>`).join('')}</div>`);
  }
  const html = [...groups.values()].sort((a, b) => (b.vid ?? -1) - (a.vid ?? -1)).map(g => `<div class="sgroup${g.vid === cur.version_id ? ' cur' : ''}">
    <div class="shead" ${g.vid ? `data-vid="${g.vid}"` : ''} title="${esc(g.code || cur.entity_name || '')}">${midEllipsis(g.code || cur.entity_name || '')}<span class="muted">${g.cards.length}</span></div>${g.cards.join('')}</div>`).join('');
  if (el.dataset.html !== html) { el.dataset.html = html; el.innerHTML = html; }  // images and scroll stay put
}
$('#shotList').addEventListener('click', e => {
  if (e.target.closest('.nfiles')) return;
  if (e.target.matches('.sthumb')) return window.open(e.target.src, '_blank', 'noopener');
  const t = e.target.closest('.snote, .shead');
  if (!t) return;
  const { vid, frame, note, sg } = t.dataset;
  if (!vid) { if (sg) window.open(`${SG_URL}/detail/Note/${sg}`, '_blank', 'noopener'); return; }
  gotoVersion(+vid, frame == null ? undefined : +frame, note ? +note : undefined, sg);
});
function gotoVersion(vid, frame, note, sgNote) {
  local();
  if (S.playing) video.pause();
  const i = S.seq.segs.findIndex(s => s.version_id === vid);
  if (i >= 0) return goSeg(i, frame ?? 0, note ? { note } : {});
  if (S.items.some(x => x.version_id === vid) || S.vinfo[vid]) return openMode(S.mode, vid, { frame, note });
  window.open(`${SG_URL}/detail/${sgNote ? `Note/${sgNote}` : `Version/${vid}`}`, '_blank', 'noopener');  // no movie to play here
}

let detailsTimer = null;
function loadDetails() {
  clearTimeout(detailsTimer);
  detailsTimer = setTimeout(async () => {  // waits out shots flying past during playback
    const cur = S.cur;
    if (!cur || S.rtab !== 'details') return;
    try {
      S.fieldDefs ||= await api('/fields');
      const vid = cur.version_id, c = S.details[vid], keys = detailKeys();
      if (!c || Date.now() - c.at > 60000 || keys.some(k => !c.asked.includes(k))) {
        const r = await api(`/versions/${vid}/details?fields=${encodeURIComponent(keys.join(','))}`);
        for (const f of r.defs) S.detailDefs[f.key] = f;
        S.details[vid] = { at: Date.now(), asked: keys, vals: r.vals };
      }
    } catch (e) { toast(e.message, true); }
    renderDetails();
    if (!$('#fieldPicker').hidden) renderFieldPicker();
  }, 250);
}
function detailValue(def, v) {
  if (v == null || v === '' || (Array.isArray(v) && !v.length)) return '<span class="muted">—</span>';
  if (def.type === 'status_list') { const st = statusOf(v); return `<span class="st" style="--c:${st.color}">${esc(st.name)}</span>`; }
  if (def.type === 'date_time') return esc(new Date(v * 1000).toLocaleString());
  if (def.type === 'checkbox') return v ? 'Yes' : 'No';
  if (def.type === 'float') return esc(String(+(+v).toFixed(3)));
  return esc(Array.isArray(v) ? v.join(', ') : String(v));
}
function renderDetails() {
  const vals = S.details[S.cur?.version_id]?.vals || {}, defs = S.fieldDefs || [];
  $('#detailList').innerHTML = detailKeys().map(k => S.detailDefs[k] || defs.find(f => f.key === k)).filter(Boolean).map(f =>
    `<div class="df" data-k="${esc(f.key)}"><span class="dl">${esc(f.name)}</span><span class="dv">${f.key in vals ? detailValue(f, vals[f.key]) : ''}</span><button class="x" title="Remove">×</button></div>`).join('');
}
function setDetailKeys(keys) {
  localStorage.rt_detail_fields = JSON.stringify(keys);
  renderDetails(); renderFieldPicker(); loadDetails();
}
// Fields as a tree: an entity field opens the fields of what it links to (Link › Shot › Cut Order)
function fieldTree(q, on) {
  const chev = open => `<svg class="chev${open ? ' open' : ''}" viewBox="0 0 10 10"><path d="M3.5 1.5L7 5 3.5 8.5" fill="none" stroke="currentColor" stroke-width="1.5"/></svg>`;
  const pad = depth => `style="padding-left:${4 + depth * 14}px"`;
  const branch = (path, depth) => (S.linkOpen.has(path) ? rows(S.linkRows[path] || [], depth) : '');
  const rows = (list, depth) => list.map(f => {
    let kids = '', exp = '';
    if (f.links?.length) {
      const single = f.links.length === 1, target = single ? `${f.key}.${f.links[0]}` : f.key;
      exp = `<button class="fx" data-path="${esc(target)}" ${single ? 'data-load="1"' : ''}>${chev(S.linkOpen.has(target))}</button>`;
      if (single) kids = branch(target, depth + 1);
      else if (S.linkOpen.has(target)) kids = f.links.map(t => { const path = `${f.key}.${t}`, inner = branch(path, depth + 2);
        return `<div class="fopt ftype" ${pad(depth + 1)}><button class="fx" data-path="${esc(path)}" data-load="1">${chev(S.linkOpen.has(path))}</button>${esc(t)}</div>${inner}`; }).join('');
    }
    const label = f.name.split(' › ').pop(), hit = !q || label.toLowerCase().includes(q);
    if (!hit && !kids) return '';
    return `<div class="fopt${on.has(f.key) ? ' on' : ''}" data-k="${esc(f.key)}" ${pad(depth)}>${svg(ICONS.check)}<span class="fn">${esc(label)}</span>${exp}</div>${kids}`;
  }).join('');
  return rows(S.fieldDefs || [], 0);
}
function renderFieldPicker() {
  $('#fieldList').innerHTML = fieldTree($('#fieldSearch').value.trim().toLowerCase(), new Set(detailKeys()));
}
async function toggleFieldBranch(fx) {
  const path = fx.dataset.path;
  if (S.linkOpen.has(path)) S.linkOpen.delete(path);
  else {
    if (fx.dataset.load && !S.linkRows[path]) {
      try { S.linkRows[path] = await api(`/fields?path=${encodeURIComponent(path)}`); } catch (err) { return toast(err.message, true); }
    }
    S.linkOpen.add(path);
  }
}
$('#btnFields').addEventListener('click', () => {
  const p = $('#fieldPicker');
  p.hidden = !p.hidden;
  if (!p.hidden) { renderFieldPicker(); $('#fieldSearch').focus(); }
});
$('#fieldSearch').addEventListener('input', renderFieldPicker);
$('#fieldSearch').addEventListener('keydown', e => e.stopPropagation());
$('#btnFieldReset').addEventListener('click', () => setDetailKeys(DETAIL_DEFAULT));
$('#fieldList').addEventListener('click', async e => {
  const fx = e.target.closest('.fx');
  if (fx) { await toggleFieldBranch(fx); return renderFieldPicker(); }
  const k = e.target.closest('.fopt')?.dataset.k, keys = detailKeys();
  if (k) setDetailKeys(keys.includes(k) ? keys.filter(x => x !== k) : [...keys, k]);
});
$('#detailList').addEventListener('click', e => {
  if (e.target.closest('.x')) setDetailKeys(detailKeys().filter(k => k !== e.target.closest('.df').dataset.k));
});

// pane widths: drag the edge, double-click to reset
for (const [id, prop, ls, min, width] of [['#leftGrip', '--lwu', 'rt_lw', 180, (ev, r) => ev.clientX - r.left], ['#rightGrip', '--rwu', 'rt_rw', 260, (ev, r) => r.right - ev.clientX]]) {
  const grip = $(id), main = $('main');
  const apply = w => main.style.setProperty(prop, `${clamp(Math.round(w), min, Math.min(720, innerWidth / 2))}px`);
  if (localStorage[ls]) apply(+localStorage[ls]);
  grip.addEventListener('pointerdown', e => {
    e.preventDefault();
    grip.setPointerCapture(e.pointerId);
    grip.classList.add('drag'); document.body.classList.add('resizing');
    const move = ev => apply(width(ev, main.getBoundingClientRect()));
    const up = () => {
      grip.classList.remove('drag'); document.body.classList.remove('resizing');
      grip.removeEventListener('pointermove', move);
      if (main.style.getPropertyValue(prop)) localStorage[ls] = parseInt(main.style.getPropertyValue(prop), 10);
    };
    grip.addEventListener('pointermove', move);
    grip.addEventListener('pointerup', up, { once: true });
  });
  grip.addEventListener('dblclick', () => { main.style.removeProperty(prop); localStorage.removeItem(ls); });
}

async function sendReply(card, top) {
  const text = card.querySelector('.reply-box textarea').value.trim();
  if (!text || !await ensureIdentity()) return;
  postNote({ parent_id: top.id, version_id: S.cur.version_id, item_id: S.cur.item_id, frame: top.frame, text });
  S.replyOpen = null;
  renderNotes();
}

function removeNote(id) {
  S.notes = S.notes.filter(n => n.id !== id && n.parent_id !== id);
  if (S.selected === id) S.selected = null;
  renderNotes(); renderTimeline(); renderOverlay(); renderSidebar();
}

// ─── composer ───────────────────────────────────────────────────────────────
// Frames the next note covers: In/Out when picked, else the drawing's frame plus the hold
function noteSpan() {
  const r = S.noteIO && ioLocal();
  if (r) return { frame: r.a, hold: r.b - r.a };
  if (S.editing || !S.cur) return { frame: S.editing ? S.editing.frame : S.frame, hold: S.hold };
  const df = draftFrames(), f = df.length === 1 ? df[0] : S.frame;
  return { frame: f, hold: draftHold(f) ?? S.hold };
}
function renderFrameChip() {
  const sp = noteSpan(), n = S.cur && !S.editing ? draftFrames().length : 0;
  $('#frameChip').textContent = !S.cur ? '' : n > 1 ? `${fmtF(Math.min(...draftFrames()))} +${n - 1}` : rangeLabel(sp.frame, noteEnd(sp));
}
function renderComposer() {
  renderFrameChip();
  const sel = $('#holdSelect');
  const h = noteSpan().hold, custom = HOLDS.some(x => x[0] === h) ? '' : `<option value="${h}">${h + 1} frames</option>`;
  sel.innerHTML = HOLDS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('') + custom + '<option value="io">In → Out</option>';
  sel.value = S.noteIO && ioLocal() ? 'io' : String(h);
  const others = S.cur ? draftFrames().filter(f => f !== S.frame) : [];
  $('#draftChips').innerHTML = others.map(f => `<span class="dchip" data-f="${f}" title="Unposted drawing">${fmtF(f)}</span>`).join('');
  const n = S.cur && !S.editing ? draftFrames().length : 0;
  $('#btnPost').textContent = S.editing ? 'Save' : n > 1 ? `Post ${n}` : 'Post';
  renderFileChips();
  $('#btnCancelEdit').hidden = !S.editing;
}
function setHold(v) {
  if (v !== -1 && S.hold !== v) S.prevHold = v;
  S.hold = v;
  S.noteIO = false;
  if (!S.editing) localStorage.rt_hold = v;
  if (!S.editing && S.cur && frameDraft(S.frame).length) { setDraftHold(S.cur.version_id, S.frame, v); saveStore(); }  // the drawing here takes it too
  renderToolbar(); renderOverlay(); renderTimeline(); renderComposer();
}
const toggleHold = () => setHold(S.hold === -1 ? (S.prevHold ?? 0) : -1);
$('#holdSelect').addEventListener('change', e => {
  if (e.target.value !== 'io') return setHold(+e.target.value);
  if (ioLocal()) { S.noteIO = true; renderComposer(); return; }
  const g = gFrame();  // no In/Out yet: half a second from here, adjustable on the timeline
  setIO(g, Math.min(g + 11, S.cur.offset + S.cur.len - 1));
  S.noteIO = true; renderComposer();
});
$('#draftChips').addEventListener('click', e => {
  const c = e.target.closest('.dchip');
  if (c) { local(); if (S.playing) video.pause(); seek(+c.dataset.f); }
});
$('#frameChip').addEventListener('click', () => { if (S.cur) { local(); if (S.playing) video.pause(); seek(noteSpan().frame); } });

$('#noteText').addEventListener('focus', () => { if (S.playing) { local(); video.pause(); } });
$('#noteText').addEventListener('input', e => {
  if (S.editing || !S.cur) return;
  S.store.texts[S.cur.version_id] = e.target.value;
  saveStore();
});
$('#noteText').addEventListener('keydown', e => {
  e.stopPropagation();
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); submitNote(); }
  if (e.key === 'Escape') { if (S.editing) cancelEdit(); e.target.blur(); }
});
$('#btnPost').addEventListener('click', submitNote);

// Attachments for the next note: uploaded straight away, tied to the note when it posts
const pendingFiles = (vid = S.cur?.version_id) => S.store.files?.[vid] || [];
function renderFileChips() {
  const el = $('#fileChips'), vid = S.cur?.version_id;
  $('#btnAttach').hidden = !can('attach') || !!S.editing;
  if (S.editing || !vid) { el.innerHTML = ''; return; }
  const fmt = b => (b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
  el.innerHTML = pendingFiles().map(f => `<span class="fchip" title="${esc(f.name)}">${svg(ICONS.clip)}<span class="fname">${midEllipsis(f.name)}</span><span class="muted">${fmt(f.size)}</span><button data-file="${f.id}">×</button></span>`).join('')
    + S.uploads.filter(u => u.vid === vid).map(u => `<span class="fchip up" title="${esc(u.name)}">${svg(ICONS.clip)}<span class="fname">${midEllipsis(u.name)}</span><span class="muted">${Math.round(u.p * 100)}%</span></span>`).join('');
}
function uploadFile(file) {
  const vid = S.cur.version_id, up = { vid, name: file.name, p: 0 };
  S.uploads.push(up); renderFileChips();
  const done = () => { S.uploads.splice(S.uploads.indexOf(up), 1); renderFileChips(); };
  const xhr = new XMLHttpRequest();
  xhr.open('POST', `${API}/files?name=${encodeURIComponent(file.name)}`);
  xhr.upload.onprogress = e => { if (e.lengthComputable) { up.p = e.loaded / e.total; renderFileChips(); } };
  xhr.onload = () => {
    if (xhr.status === 200) { ((S.store.files ||= {})[vid] ||= []).push(JSON.parse(xhr.responseText)); saveStore(); }
    else { let m = xhr.status === 413 ? 'File too large' : `Upload failed (${xhr.status})`; try { m = JSON.parse(xhr.responseText).detail || m; } catch (e) { /* the proxy answered, not the app */ } toast(m, true); }
    done();
  };
  xhr.onerror = () => { toast(`Upload failed: ${file.name}`, true); done(); };
  xhr.send(file);
}
async function attachFiles(files) {
  files = [...files];
  if (!files.length || !can('attach') || !S.cur || S.editing || !await ensureIdentity()) return;
  files.forEach(uploadFile);
}
$('#btnAttach').addEventListener('click', () => $('#fileInput').click());
$('#fileInput').addEventListener('change', e => { attachFiles(e.target.files); e.target.value = ''; });
$('#fileChips').addEventListener('click', async e => {
  const id = +e.target.closest('button[data-file]')?.dataset.file, vid = S.cur?.version_id;
  if (!id) return;
  S.store.files[vid] = pendingFiles().filter(f => f.id !== id);
  saveStore(); renderFileChips();
  api(`/files/${id}`, { method: 'DELETE' }).catch(() => {});
});
$('#noteText').addEventListener('paste', e => { if (e.clipboardData?.files?.length) { e.preventDefault(); attachFiles(e.clipboardData.files); } });
{
  const panel = $('#notesPanel');
  const hasFiles = e => [...(e.dataTransfer?.types || [])].includes('Files') && can('attach') && !S.editing;
  panel.addEventListener('dragover', e => { if (hasFiles(e)) { e.preventDefault(); panel.classList.add('dropping'); } });
  panel.addEventListener('dragleave', e => { if (!panel.contains(e.relatedTarget)) panel.classList.remove('dropping'); });
  panel.addEventListener('drop', e => { if (hasFiles(e)) { e.preventDefault(); panel.classList.remove('dropping'); attachFiles(e.dataTransfer.files); } });
}
$('#btnCancelEdit').addEventListener('click', () => cancelEdit());

async function submitNote() {
  if (!S.cur) return;
  const text = $('#noteText').value.trim(), { frame, hold } = noteSpan(), usedIO = S.noteIO && !!ioLocal();
  if (S.editing) {
    const shapes = S.editing.shapes;
    if (!text && !shapes.length) return;
    try {
      upsertNote(await api(`/notes/${S.editing.id}`, { method: 'PATCH', body: { text, shapes, hold, frame } }));
      delete S.hist[`e${S.editing.id}`];
      endEdit();
      finishComposer();
    } catch (e) { toast(e.message, true); }
    return;
  }
  // Every unposted drawing on this version goes out together, one note per drawn frame. The comment
  // rides on the drawing you're looking at, else the first one. A single note keeps the picked duration.
  const df = draftFrames().sort((a, b) => a - b), vid = S.cur.version_id, files = pendingFiles();
  if (!text && !df.length && !files.length) return;
  if (S.uploads.some(u => u.vid === vid)) return toast('Still uploading');
  if (!await ensureIdentity()) return;
  const posts = df.length > 1 ? df.map(f => ({ frame: f, hold: draftHold(f) ?? S.hold, text: '', shapes: drafts()[f] }))
    : [{ frame: df.length && !usedIO ? df[0] : frame, hold, text, shapes: df.length ? drafts()[df[0]] : [] }];
  const carrier = posts.find(p => p.frame === S.frame) || posts[0];
  if (df.length > 1) carrier.text = text;
  if (files.length) { carrier.files = files; carrier.file_ids = files.map(f => f.id); }
  for (const p of posts) postNote({ item_id: S.cur.item_id, version_id: vid, ...p, shapes: clone(p.shapes) });  // outbox first, then clear
  for (const f of df) { delete drafts()[f]; delete S.hist[`${vid}:${f}`]; }
  delete S.store.texts[vid]; delete S.store.holds?.[vid]; delete S.store.files?.[vid];
  S.draftSel = null;
  if (usedIO) { S.io = null; S.noteIO = false; applyAudio(); }
  saveStore();
  finishComposer();
}

function finishComposer() {
  $('#noteText').value = (S.cur && !S.editing && S.store.texts[S.cur.version_id]) || '';
  S.pick = null;
  afterDraftChange();
  renderNotes(); renderSidebar();
}

function startEdit(n, focus = true) {
  if (n.parent_id) {  // replies: text only
    const text = prompt('Edit reply', n.text);
    if (text == null) return;
    api(`/notes/${n.id}`, { method: 'PATCH', body: { text } }).then(x => { upsertNote(x); renderNotes(); }, e => toast(e.message, true));
    return;
  }
  if (S.playing) video.pause();
  if (S.editing) endEdit();
  S.editing = { id: n.id, frame: n.frame, shapes: clone(n.shapes || []), was: { hold: S.hold, io: S.io, noteIO: S.noteIO } };
  S.selected = n.id;
  if (n.hold > 0 && !HOLDS.some(h => h[0] === n.hold)) {
    S.io = { a: S.cur.offset + n.frame, b: S.cur.offset + noteEnd(n) };
    S.noteIO = true;
  } else { S.hold = n.hold; S.noteIO = false; }
  applyAudio();
  seek(n.frame);
  $('#noteText').value = n.text;
  afterDraftChange();
  renderNotes(); renderToolbar();
  if (focus) $('#noteText').focus();
}
function endEdit() {  // back to the composer settings from before the edit
  Object.assign(S, S.editing.was);
  S.editing = null;
  applyAudio(); renderToolbar();
}
function cancelEdit(render = true) {
  if (!S.editing) return;
  endEdit();
  if (render) finishComposer();
}

// ─── toolbar ────────────────────────────────────────────────────────────────
function renderToolbar() {
  $('#tools').innerHTML = TOOLS.map(([t, title]) => `<button class="icon${S.tool === t ? ' active' : ''}" data-tool="${t}" title="${title}">${svg(ICONS[t])}</button>`).join('');
  const custom = !COLORS.includes(S.color) && !S.slots.includes(S.color);
  const swatches = document.createElement('div'), box = $('#colors');
  swatches.innerHTML = COLORS.map((c, i) => `<button class="swatch${S.color === c ? ' active' : ''}" data-color="${c}" style="background:${c}" title="${i + 1}"></button>`).join('')
    + '<i class="csep"></i>'
    + S.slots.map((c, i) => `<button class="swatch slot${c ? '' : ' empty'}${c && S.color === c ? ' active' : ''}" data-slot="${i}" ${c ? `style="background:${c}"` : ''}></button>`).join('')
    + `<button class="swatch picker${custom ? ' active' : ''}" data-picker="1" style="--c:${S.color}" title="Colour"></button>`;
  // Updated in place: a click lands here, and a rebuilt button would never get the second click of a double-click
  if (box.children.length !== swatches.children.length) box.replaceChildren(...swatches.children);
  else [...swatches.children].forEach((n, i) => { box.children[i].className = n.className; box.children[i].setAttribute('style', n.getAttribute('style') || ''); });
  syncSliders();
  stage.className = { none: 'tool-none', text: 'tool-text', eraser: 'tool-eraser' }[S.tool] || 'tool-draw';
  $('#btnOnion').classList.toggle('active', S.onion);
  $('#onionN').hidden = !S.onion;
  if (document.activeElement !== $('#onionN')) $('#onionN').value = S.onionN;
  $('#btnHold').classList.toggle('active', S.hold === -1);
  $('#btnDrawings').classList.toggle('active', S.showDrawings);
  $('#btnDrawings').innerHTML = svg(S.showDrawings ? ICONS.eye : ICONS.eyeOff);
}
function setTool(t) { S.tool = localStorage.rt_tool = t; if (t !== 'none') S.pick = null; S.hover = null; canvas.style.cursor = ''; renderToolbar(); renderOverlay(); }
function setColor(c) {
  S.color = localStorage.rt_color = c;
  if (!editPicked(s => ({ ...s, c })) && (S.tool === 'none' || S.tool === 'eraser')) setTool('pen');
  renderToolbar();
}
const opacityProp = () => (S.opacity < 1 ? { o: S.opacity } : {});
const pickedShape = () => (S.pick != null && frameDraft()[S.pick]) || null;
// The sliders show the selected drawing when there is one, else what the next drawing will use
function syncSliders() {
  const p = pickedShape(), size = p ? (p.t === 'text' ? p.s / 6 : p.w) ?? S.size : S.size, op = p ? p.o ?? 1 : S.opacity;
  for (const [el, v] of [[$('#sizeRange'), size], [$('#opacityRange'), op]]) {
    if (+el.value !== v && document.activeElement !== el) el.value = v;
    fillRange(el);
  }
  const dot = $('#brushDot'), px = clamp(size * 400, 2, 14);
  Object.assign(dot.style, { width: `${px}px`, height: `${px}px`, background: S.color, opacity: op });
}
// A slider drag on a selected drawing is one undo step
function sliderEdit(fn) {
  const arr = frameDraft(), s = pickedShape();
  if (!s) return;
  if (!S.sliderDrag) { checkpoint(); S.sliderDrag = true; }
  arr[S.pick] = fn(s);
  afterDraftChange();
}
$('#sizeRange').addEventListener('input', e => {
  S.size = +e.target.value; localStorage.rt_bsize = S.size;
  sliderEdit(s => (s.t === 'text' ? { ...s, s: Math.max(0.012, S.size * 6) } : { ...s, w: S.size }));
  syncSliders(); renderOverlay();
});
$('#opacityRange').addEventListener('input', e => {
  S.opacity = +e.target.value; localStorage.rt_opacity = S.opacity;
  sliderEdit(s => { const { o, ...rest } = s; return S.opacity < 1 ? { ...rest, o: S.opacity } : rest; });
  syncSliders(); renderOverlay();
});
for (const id of ['#sizeRange', '#opacityRange']) {
  $(id).addEventListener('change', e => { S.sliderDrag = false; e.target.blur(); });
  $(id).addEventListener('keydown', e => e.stopPropagation());
}
// Native colour picker, for any colour or for one of the three custom slots
const colorInput = $('#colorInput');
function openPicker(slot, chip) {
  S.pickerSlot = slot;
  colorInput.value = (slot != null && S.slots[slot]) || (/^#[0-9a-f]{6}$/i.test(S.color) ? S.color : '#ffffff');
  // The browser opens its picker at the input, so the invisible input goes over the chip first
  const r = chip.getBoundingClientRect();
  Object.assign(colorInput.style, { left: `${r.left}px`, top: `${r.top}px`, width: `${r.width}px`, height: `${r.height}px` });
  colorInput.click();
}
colorInput.addEventListener('input', () => {
  if (S.pickerSlot != null) { S.slots[S.pickerSlot] = colorInput.value; localStorage.rt_slots = JSON.stringify(S.slots); }
  S.color = colorInput.value;
  renderToolbar();
});
colorInput.addEventListener('change', () => setColor(colorInput.value));
$('#toolbar').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  if (b.dataset.tool) setTool(b.dataset.tool);
  if (b.dataset.color) setColor(b.dataset.color);
  if (b.dataset.picker) openPicker(null, b);
  if (b.dataset.slot) { const c = S.slots[+b.dataset.slot]; c ? setColor(c) : openPicker(+b.dataset.slot, b); }
});
$('#colors').addEventListener('dblclick', e => { const b = e.target.closest('[data-slot]'); if (b) openPicker(+b.dataset.slot, b); });
$('#colors').addEventListener('contextmenu', e => { const b = e.target.closest('[data-slot]'); if (b) { e.preventDefault(); openPicker(+b.dataset.slot, b); } });
$('#btnUndo').addEventListener('click', undo);
$('#btnRedo').addEventListener('click', redo);
$('#btnClear').addEventListener('click', clearDraft);
function toggleOnion() { S.onion = !S.onion; localStorage.rt_onion = S.onion ? '1' : '0'; renderToolbar(); renderOverlay(); }
function toggleDrawings() { S.showDrawings = !S.showDrawings; localStorage.rt_show = S.showDrawings ? '1' : '0'; renderToolbar(); renderOverlay(); }
$('#btnOnion').addEventListener('click', toggleOnion);
$('#onionN').addEventListener('input', e => {
  const n = parseInt(e.target.value, 10);
  if (n >= 1) { S.onionN = Math.min(n, 48); localStorage.rt_onion_n = S.onionN; renderOverlay(); }
});
$('#onionN').addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Enter' || e.key === 'Escape') e.target.blur(); });
$('#btnHold').addEventListener('click', toggleHold);
$('#btnDrawings').addEventListener('click', toggleDrawings);

// ─── transport ──────────────────────────────────────────────────────────────
const on = (id, fn) => $(id).addEventListener('click', () => { local(); fn(); });
on('#btnPlay', togglePlay);
on('#btnBack', () => step(-1));
on('#btnFwd', () => step(1));
on('#btnFirst', () => { if (S.playing) video.pause(); seek(0); });
on('#btnLast', () => { if (S.playing) video.pause(); seek(nFrames() - 1); });
on('#btnPrevNote', () => jumpNote(-1));
on('#btnNextNote', () => jumpNote(1));
on('#btnLoop', () => { S.loop = !S.loop; localStorage.rt_loop = S.loop ? '1' : '0'; applyAudio(); preloadNext(); $('#btnLoop').classList.toggle('active', S.loop); });
on('#btnMute', () => { S.muted = !S.muted; applyAudio(); $('#btnMute').innerHTML = svg(S.muted ? ICONS.muted : ICONS.sound); });
on('#btnFull', () => { document.fullscreenElement ? document.exitFullscreen() : $('#viewer').requestFullscreen(); });
$('#btnExport').addEventListener('click', e => {
  if (!ready()) return;
  const n = topNotes().find(x => x.id === S.selected && x.frame === S.frame), r = e.currentTarget.getBoundingClientRect();
  const get = url => { const a = document.createElement('a'); a.href = url; a.download = ''; a.click(); };
  openMenu(r.left, r.top, [
    ...S.cur.item_id ? [{ html: `Frame <span class="muted">PNG</span>`, run: () => window.open(`${API}/frame/${S.cur.item_id}/${S.frame}.png${n ? `?note=${n.id}` : ''}`, '_blank') }] : [],
    { html: `Movie <span class="muted">MP4</span>`, run: () => get(`${API}/media/${S.cur.version_id}/proxy.mp4?dl=1`) },
    { html: 'Export…', run: () => openExport() }]);
});
// PDF report, annotated frames (ZIP) or notes (CSV), of this version, the selection or everything
const EXPORTS = [...APP.pdf ? [['pdf', 'PDF report']] : [], ['zip', 'Frames ZIP'], ['csv', 'Notes CSV']];
function openExport(selected = null) {
  const curId = S.cur?.item_id, picked = selected || (S.multi.size > 1 ? [...S.multi] : null);
  let scope = picked ? 'sel' : curId ? 'cur' : 'all', busy = false;
  const ids = () => (scope === 'sel' ? picked : scope === 'cur' ? [curId] : []);
  const list = () => (scope === 'all' ? S.items : S.items.filter(i => ids().includes(i.id)));
  const tops = i => itemNoteRows(i.id).filter(n => !n.parent_id);
  openModal(`<h2>Export</h2>
    <div class="seg wide" id="expScope">
      ${picked ? '<button data-s="sel">Selected</button>' : ''}
      <button data-s="cur" ${curId ? '' : 'disabled'}>This version</button>
      <button data-s="all">All versions</button></div>
    <div class="row exp-sum muted" id="expSum"></div>
    <div class="row exp-go">${EXPORTS.map(([k, l]) => `<button data-k="${k}" id="exp_${k}">${l}</button>`).join('')}</div>
    <div class="foot"><button id="expClose">Close</button></div>`);
  const draw = () => {
    $$('#expScope button').forEach(b => b.classList.toggle('active', b.dataset.s === scope));
    const notes = list().flatMap(tops), frames = notes.filter(n => n.shapes?.length).length;
    $('#expSum').textContent = `${list().length} version${list().length === 1 ? '' : 's'} · ${notes.length} note${notes.length === 1 ? '' : 's'} · ${frames} frame${frames === 1 ? '' : 's'}`;
    for (const b of $$('.exp-go button')) b.disabled = busy || !notes.length || (b.dataset.k === 'zip' && !frames);
  };
  draw();
  $('#expClose').onclick = closeModal;
  $('#expScope').addEventListener('click', e => { const b = e.target.closest('button[data-s]'); if (b && !b.disabled && !busy) { scope = b.dataset.s; draw(); } });
  $('.exp-go').addEventListener('click', async e => {
    const b = e.target.closest('button[data-k]');
    if (!b || b.disabled) return;
    const label = b.textContent;
    busy = true; draw(); b.textContent = 'Starting…';
    try {
      const { id } = await api('/export', { method: 'POST', body: { kind: b.dataset.k, item_ids: ids() } });
      for (;;) {
        await new Promise(res => setTimeout(res, 700));
        const j = await api(`/export/${id}`);
        if (j.status === 'error') throw new Error(j.error || 'Export failed');
        if (j.status === 'done') break;
        if (b.isConnected) b.textContent = j.total ? `${j.done} / ${j.total}` : 'Starting…';
      }
      const a = document.createElement('a'); a.href = `${API}/export/${id}/file`; a.download = ''; a.click();
    } catch (err) { toast(err.message, true); }
    busy = false;
    if (b.isConnected) { b.textContent = label; draw(); }
  });
}
function markIn() { if (S.cur) { const g = gFrame(); setIO(g, S.io && S.io.b >= g ? S.io.b : S.cur.offset + S.cur.len - 1); } }
function markOut() { if (S.cur) { const g = gFrame(); setIO(S.io && S.io.a <= g ? S.io.a : S.cur.offset, g); } }
on('#btnIn', markIn);
on('#btnOut', markOut);
on('#btnClearIO', () => setIO(null));
// range inputs paint their filled part from --p
const fillRange = el => el.style.setProperty('--p', `${(el.value - el.min) / (el.max - el.min) * 100}%`);
$('#volume').addEventListener('input', e => { S.volume = +e.target.value; fillRange(e.target); applyAudio(); });
$('#speed').addEventListener('change', e => { local(); S.rate = +e.target.value; applyAudio(); broadcastPresent(); });
$('#frameInput').addEventListener('keydown', e => {
  e.stopPropagation();
  if (e.key === 'Enter') {
    const t = e.target.value.trim(), parts = t.split(/[:;.]/).map(Number);
    const f = parts.length > 1 ? parts.reduce((a, x, k) => k < parts.length - 1 ? a * 60 + x : a * Math.max(1, Math.round(fps())) + x, 0)
      : parseInt(t, 10) - (S.cur?.first_frame ?? 1);
    if (!isNaN(f)) { local(); if (S.playing) video.pause(); seek(f); }
    e.target.blur();
  }
  if (e.key === 'Escape') e.target.blur();
});
$('#frameInput').addEventListener('blur', onFrame);
$('#tc').addEventListener('click', () => {
  S.tcMode = !S.tcMode; localStorage.rt_tc = S.tcMode ? '1' : '0';
  $('#frameInput').classList.toggle('tc', S.tcMode);
  onFrame(); renderNotes(); renderTimeline(); renderComposer();
});
$('#frameInput').classList.toggle('tc', S.tcMode);

// ─── keyboard ───────────────────────────────────────────────────────────────
const KEYS = [['Space / K', 'Play / pause'], ['← →', 'Previous / next frame (Shift ×10)'], ['↑ ↓', 'Previous / next note'],
  ['Home / End', 'First / last frame'], ['[ ]', 'Previous / next version'], ['I / O · Shift-drag timeline', 'In / Out'], ['Esc', 'Clear In / Out and selection'],
  ['V B P N A R C T E', 'Select, brush, marker, line, arrow, rectangle, ellipse, text, eraser'], ['1–9', 'Colour'],
  ['Shift + drag', 'Square / circle · keep proportions'], ['Double-click text', 'Edit text'], ['Delete', 'Delete selected shape'],
  ['Ctrl+Z / Ctrl+Shift+Z', 'Undo / redo'], ['Ctrl+X / C / V · right-click', 'Cut, copy, paste a drawing to another frame'], ['U', 'Onion skin'], ['G', 'Hold until next drawing'], ['H', 'Show drawings'],
  ['Wheel · + − 0', 'Zoom · fit'], ['Wheel on timeline', 'Zoom timeline (Shift pans)'], ['Middle-drag', 'Pan'],
  ['Enter', 'Comment'], ['Ctrl+Enter', 'Post'], ['Cmd/Ctrl · Shift + click', 'Select versions to play back to back'],
  ['\\', 'Hide / show panels'], ['M / F', 'Mute / fullscreen']];

document.addEventListener('keydown', e => {
  if (e.target.matches('input, textarea, select') || !$('#modalBack').hidden) {
    if (e.key === 'Escape' && !$('#modalBack').hidden) closeModal();
    return;
  }
  const k = e.key, ctrl = e.ctrlKey || e.metaKey;
  const handled = () => e.preventDefault();
  if (ctrl && k.toLowerCase() === 'z') { handled(); e.shiftKey ? redo() : undo(); return; }
  if (ctrl && k.toLowerCase() === 'y') { handled(); redo(); return; }
  if (ctrl && !e.shiftKey && 'xcv'.includes(k.toLowerCase()) && !String(getSelection())) {
    handled();
    ({ x: clipCut, c: clipCopy, v: clipPaste })[k.toLowerCase()]();
    return;
  }
  if (ctrl) return;
  switch (k) {
    case ' ': case 'k': case 'K': handled(); local(); togglePlay(); break;
    case 'ArrowLeft': case ',': handled(); local(); step(e.shiftKey ? -10 : -1); break;
    case 'ArrowRight': case '.': handled(); local(); step(e.shiftKey ? 10 : 1); break;
    case 'ArrowUp': handled(); local(); jumpNote(-1); break;
    case 'ArrowDown': handled(); local(); jumpNote(1); break;
    case 'Home': handled(); $('#btnFirst').click(); break;
    case 'End': handled(); $('#btnLast').click(); break;
    case 'j': case 'J': local(); step(-1); break;
    case 'l': case 'L': local(); if (video.paused) togglePlay(); break;
    case '[': navSeg(-1); break;
    case ']': navSeg(1); break;
    case 'i': case 'I': markIn(); break;
    case 'o': case 'O': markOut(); break;
    case 'u': case 'U': toggleOnion(); break;
    case 'g': case 'G': toggleHold(); break;
    case '\\': togglePanel('all'); break;
    case 'm': case 'M': $('#btnMute').click(); break;
    case 'f': case 'F': $('#btnFull').click(); break;
    case 'h': case 'H': toggleDrawings(); break;
    case '+': case '=': zoomAt(1.25); break;
    case '-': case '_': zoomAt(0.8); break;
    case '0': setView('screen'); break;
    case 'Enter': handled(); $('#noteText').focus(); break;
    case 'Delete': case 'Backspace': handled(); deletePicked(); break;
    case 'Escape': S.pick = null; S.selected = null; S.draftSel = null; S.rangeNote = null; S.noteSel.clear(); $('#menu').hidden = true; setIO(null); renderNotes(); break;
    case '?': showHelp(); break;
    default: {
      const t = TOOLS.find(x => x[2] === k.toLowerCase());
      if (t) setTool(t[0]);
      else if (/^[1-9]$/.test(k)) setColor(COLORS[+k - 1]);
    }
  }
});
function navSeg(d) {
  local();
  if (S.seq.segs.length > 1) return goSeg(clamp(S.seq.i + d, 0, S.seq.segs.length - 1), 0);
  const i = S.items.findIndex(x => x.version_id === S.anchor);
  const n = S.items[clamp(i + d, 0, S.items.length - 1)];
  if (n && n.version_id !== S.anchor) openMode(S.mode, n.version_id);
}

// ─── modals ─────────────────────────────────────────────────────────────────
function openModal(html, cls = '') { $('#modal').className = cls; $('#modal').innerHTML = html; $('#modalBack').hidden = false; }
function closeModal() { $('#modalBack').hidden = true; $('#modal').innerHTML = ''; S.modalResolve?.(null); S.modalResolve = null; }
$('#modalBack').addEventListener('mousedown', e => { if (e.target.id === 'modalBack' && !S.modalLocked) closeModal(); });
window.closeModal = closeModal;

function showHelp() {
  openModal(`<h2>Shortcuts</h2><table>${KEYS.map(([k, d]) => `<tr><td><kbd>${k}</kbd></td><td>${d}</td></tr>`).join('')}</table>
    <div class="foot"><button onclick="closeModal()">Close</button></div>`);
}

// identity: resolves true once the user is signed in with ShotGrid or has joined as a guest
async function ensureIdentity() {
  if (S.me) return true;
  return new Promise(resolve => {
    S.modalResolve = v => resolve(!!v);
    S.modalLocked = true;
    openModal(`<h2>Who's reviewing?</h2>
      <button class="primary big" id="btnSG">Sign in with ShotGrid</button>
      <div class="or">or</div>
      <div class="row"><input type="text" id="guestName" placeholder="Your name" style="flex:1"><button id="btnGuest">Join as guest</button></div>`);
    const done = async user => {
      S.me = user;
      S.modalLocked = false;
      S.modalResolve = null;
      closeModal();
      await reload();
      reconnectWS();
      await pullStore();
      flushOutbox();
      resolve(true);
    };
    const guest = async () => {
      const name = $('#guestName').value.trim();
      if (!name) return $('#guestName').focus();
      try { done((await api('/api/identify', { method: 'POST', body: { token: TOKEN, guest_name: name } })).user); } catch (e) { toast(e.message, true); }
    };
    $('#btnSG').addEventListener('click', async e => {
      const b = e.currentTarget;
      b.disabled = true; b.textContent = 'Waiting for ShotGrid…';
      try { await done(await signInWithShotGrid()); } catch (err) { toast(err.message, true); b.disabled = false; b.textContent = 'Sign in with ShotGrid'; }
    });
    $('#btnGuest').addEventListener('click', guest);
    $('#guestName').addEventListener('keydown', e => { if (e.key === 'Enter') guest(); });
  });
}
// Verified-only actions: offer ShotGrid sign-in in place
async function ensureVerified() {
  if (S.me?.v) return true;
  try { S.me = await signInWithShotGrid(); } catch (e) { toast(e.message, true); return false; }
  await reload(); reconnectWS(); await pullStore(); flushOutbox();
  return true;
}

function renderUser() {
  const u = S.me;
  $('#userChip').innerHTML = u ? `${avatar(u.name, '', u.id)}<span>${esc(u.name)}</span>` : '<button>Sign in</button>';
}
$('#userChip').addEventListener('click', async () => {
  if (S.me && !confirm(`Signed in as ${S.me.name}. Switch user?`)) return;
  if (S.me) { await api('/api/logout', { method: 'POST' }); return location.reload(); }  // back through the sign-in this address asks for
  ensureIdentity();
});

// add versions
let filtersCache = null;
$('#btnAdd').addEventListener('click', async () => {
  if (!await ensureVerified()) return;
  openModal(`<h2>Add versions</h2>
    <div class="row"><input type="search" id="fq" placeholder="Search" style="flex:1">
      <select id="fep" ${S.cutLabel ? '' : 'hidden'}><option value="">${esc(S.cutLabel || '')}</option></select><select id="ftask"><option value="">Task</option></select>
      <select id="fst"><option value="">Status</option>${S.statuses.map(s => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join('')}</select></div>
    <div class="vgrid" id="vgrid"></div>
    <div class="foot"><button onclick="closeModal()">Cancel</button><button class="primary" id="btnDoAdd" disabled>Add</button></div>`, 'wide');
  const chosen = new Set(), have = new Set(S.items.map(i => i.version_id));
  let results = [], timer, seqNo = 0;
  const drawGrid = () => {
    $('#vgrid').innerHTML = results.map(v => {
      const st = statusOf(v.sg_status), h = have.has(v.version_id);
      return `<div class="vcard${h ? ' have' : ''}${chosen.has(v.version_id) ? ' sel' : ''}" data-id="${v.version_id}">
        <div class="vthumb" ${v.image ? `style="background-image:url('${esc(v.image)}')"` : ''}>${h ? `<span>${svg(ICONS.check)}</span>` : ''}</div>
        <div class="vcode" title="${esc(v.version_code)}">${midEllipsis(v.version_code)}</div>
        <div class="vsub"><i class="dot" style="background:${st.color}" title="${esc(st.name)}"></i>${esc([v.task_name, v.artist_name].filter(Boolean).join(' · '))}</div></div>`;
    }).join('');
    $('#btnDoAdd').disabled = !chosen.size;
    $('#btnDoAdd').textContent = chosen.size ? `Add ${chosen.size}` : 'Add';
  };
  const run = async () => {
    const qs = new URLSearchParams({ q: $('#fq').value.trim() });
    for (const [id, k] of [['#fep', 'episode_id'], ['#ftask', 'task'], ['#fst', 'status']]) if ($(id).value) qs.set(k, $(id).value);
    const n = ++seqNo;
    try { const r = await api(`/search?${qs}`); if (n === seqNo) { results = r; drawGrid(); } } catch (e) { toast(e.message, true); }
  };
  const later = () => { clearTimeout(timer); timer = setTimeout(run, 300); };
  $('#fq').addEventListener('input', later);
  for (const id of ['#fep', '#ftask', '#fst']) $(id).addEventListener('change', run);
  $('#fq').focus();
  $('#vgrid').addEventListener('click', e => {
    const c = e.target.closest('.vcard:not(.have)');
    if (!c) return;
    const id = +c.dataset.id;
    chosen.has(id) ? chosen.delete(id) : chosen.add(id);
    drawGrid();
  });
  $('#btnDoAdd').addEventListener('click', async () => {
    try { await api('/items', { method: 'POST', body: { version_ids: [...chosen] } }); closeModal(); await reload(); toast(`Added ${chosen.size}`); }
    catch (e) { toast(e.message, true); }
  });
  try {
    filtersCache ||= await api('/filters');
    $('#fep').innerHTML += filtersCache.episodes.map(x => `<option value="${x.id}" ${x.id === S.seq.ep ? 'selected' : ''}>${esc(x.code)}</option>`).join('');
    $('#ftask').innerHTML += filtersCache.tasks.map(t => `<option>${esc(t)}</option>`).join('');
    if (S.seq.ep) run();
  } catch (e) { /* free-text search still works */ }
});

// publish
$('#btnPublish').addEventListener('click', () => openPublish());
async function openPublish(selected = null) {
  if (!await ensureVerified()) return;
  const pend = id => itemNoteRows(id).filter(pending).length;
  const curId = S.cur?.item_id, total = S.items.reduce((a, i) => a + pend(i.id), 0);
  let scope = selected ? 'sel' : curId && pend(curId) ? 'cur' : 'all';
  openModal(`<h2>Publish to SG</h2>
    <div class="seg wide" id="pubScope">
      ${selected ? `<button data-s="sel">Selected <b>${selected.reduce((a, i) => a + pend(i), 0)}</b></button>` : ''}
      <button data-s="cur" ${curId ? '' : 'disabled'}>This version <b>${curId ? pend(curId) : 0}</b></button>
      <button data-s="all">All versions <b>${total}</b></button></div>
    ${APP.note_types.length ? `<div class="row"><select id="pubType"><option value="">Note type</option>${APP.note_types.map(t => `<option>${esc(t)}</option>`).join('')}</select></div>` : ''}
    <div id="pubSummary"></div>
    <div id="pubResult"></div>
    <div class="foot"><button onclick="closeModal()">Cancel</button><button class="primary" id="btnDoPublish">Publish</button></div>`);
  const scopeIds = () => (scope === 'sel' ? selected : scope === 'cur' ? [curId] : S.items.map(i => i.id));
  // What this scope will send, version by version; the same rules as the server (media must be ready to attach frames)
  const drawScope = () => {
    $$('#pubScope button').forEach(b => b.classList.toggle('active', b.dataset.s === scope));
    const ids = scopeIds(), plural = (n, one, many) => (n ? `${n} ${n === 1 ? one : many}` : '');
    const rows = S.items.filter(i => ids.includes(i.id)).map(i => {
      const p = itemNoteRows(i.id).filter(pending), notes = p.filter(n => !n.parent_id);
      return { i, notes: notes.length, frames: notes.filter(n => n.shapes?.length).length, replies: p.length - notes.length, files: p.reduce((a, n) => a + (n.files?.length || 0), 0) };
    }).filter(r => r.notes || r.replies);
    $('#pubSummary').innerHTML = rows.map(({ i, notes, frames, replies, files }) => `<div class="psum"><span class="sdot" style="--c:${statusOf(i.sg_status).color}"></span>
      <span class="nm" title="${esc(i.version_code)}">${midEllipsis(i.version_code)}</span>
      ${S.media[i.version_id]?.status === 'ready' ? '' : '<span class="res-err">Media not ready</span>'}
      <span class="ct">${[plural(notes, 'note', 'notes'), plural(frames, 'frame', 'frames'), plural(replies, 'reply', 'replies'), plural(files, 'file', 'files')].filter(Boolean).join(' · ')}</span></div>`).join('');
    $('#btnDoPublish').disabled = !rows.length;
  };
  drawScope();
  $('#pubScope').addEventListener('click', e => { const b = e.target.closest('button[data-s]'); if (b && !b.disabled) { scope = b.dataset.s; drawScope(); } });
  $('#btnDoPublish').addEventListener('click', async () => {
    const btn = $('#btnDoPublish');
    if (btn.dataset.done) return closeModal();
    const item_ids = scopeIds();
    $('#pubSummary').innerHTML = '';
    btn.disabled = true; btn.textContent = 'Publishing…'; S.modalLocked = true;
    try {
      const { id } = await api('/publish', { method: 'POST', body: { item_ids, note_type: $('#pubType')?.value || null } });
      let results;
      for (;;) {
        await new Promise(res => setTimeout(res, 800));
        const j = await api(`/publish/${id}`);
        if (j.status === 'error') throw new Error(j.error || 'Publish failed');
        if (j.status === 'done') { results = j.results; break; }
        if (j.total) btn.textContent = `Publishing ${j.done} / ${j.total}`;
      }
      $('#pubResult').innerHTML = results.length ? results.map(r => r.error ? `<div class="res-err">✗ ${esc(r.version_code || '')} — ${esc(r.error)}</div>`
        : `<div class="res-ok">✓ ${esc(r.version_code)} — ${r.notes} note${r.notes === 1 ? '' : 's'}, ${r.replies} repl${r.replies === 1 ? 'y' : 'ies'}, ${r.attachments} attachment${r.attachments === 1 ? '' : 's'}</div>`).join('')
        : '<div class="muted">Nothing new</div>';
      btn.textContent = 'Done'; btn.dataset.done = '1'; btn.disabled = false;
    } catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = 'Publish'; }
    S.modalLocked = false;
  });
}

// ─── realtime: presence, live notes, present/follow ─────────────────────────
let ws = null, wsRetry = 0, wsTimer = null;
function wsSend(m) { if (ws?.readyState === 1) ws.send(JSON.stringify(m)); }
function reconnectWS() { if (ws) { ws.onclose = null; ws.close(); } connectWS(); }
function connectWS() {
  if (!S.me) return;
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/${encodeURIComponent(TOKEN)}`);
  ws.onopen = () => {
    if (wsRetry > 0) { reload().catch(() => {}); flushOutbox(); }  // catch up on anything missed while disconnected
    wsRetry = 0;
    wsSend({ type: 'view', item_id: S.cur?.item_id ?? null });
    if (S.presenting) { broadcastPresent(); broadcastDraft(); }
  };
  ws.onclose = ev => {
    if (ev.code === 4410) return linkGone('This link has expired');
    if (wsRetry) api('').catch(e => {
      if (e.status === 401) location.reload();
      else if ([403, 404, 410].includes(e.status)) { clearTimeout(wsTimer); linkGone(e.message); }
    });
    clearTimeout(wsTimer); wsTimer = setTimeout(connectWS, Math.min(15000, 1000 * 2 ** wsRetry++)); };
  ws.onmessage = ev => handleWS(JSON.parse(ev.data));
}

function handleWS(m) {
  switch (m.type) {
    case 'welcome': S.cid = m.cid; break;
    case 'presence': {
      S.presence = m.users;
      const presenter = m.users.find(u => u.presenting && u.cid !== S.cid);
      if (presenter && !S.following && S.optedOut !== presenter.cid) S.following = presenter.cid;
      if (S.following && !m.users.some(u => u.cid === S.following && u.presenting)) S.following = null;
      renderPresence(); renderSidebar(); placePointer();
      break;
    }
    case 'note': {
      const isNew = !S.notes.some(n => n.id === m.note.id);
      upsertNote(m.note);
      renderNotes(); renderTimeline(); renderOverlay(); renderSidebar();
      if (isNew && !isMine(m.note)) toast(`${m.note.author_name} · ${S.items.find(i => i.id === m.note.item_id)?.version_code || ''}`);
      break;
    }
    case 'note_deleted': removeNote(m.id); break;
    case 'media': S.media[m.version_id] = m.media; mediaChanged([m.version_id]); if (m.media?.status === 'cleared') ensureSeqMedia(); break;
    case 'status': applyStatus(m.version_id, m.status); break;
    case 'items': reload(); break;
    case 'published':
      S.notes = m.notes; S.items = sortItems(m.items);
      rebuildSegs();
      renderAll();
      if (m.by !== S.me?.name) toast(`${m.by} published to ShotGrid`);
      break;
    case 'present':
      if (S.presenting) { S.presenting = false; renderPresence(); toast(`${m.name} is presenting`); }
      if (S.optedOut === m.from) break;
      S.following = m.from;
      applyPresent(m.state);
      renderPresence();
      break;
    case 'present_stop':
      if (S.following === m.from) S.following = null;
      if (S.optedOut === m.from) S.optedOut = null;
      delete S.remoteDrafts[m.from];
      S.remotePtrs = {};
      renderPresence(); renderOverlay(); placePointer();
      break;
    case 'pointer': S.remotePtrs[m.from] = m; placePointer(); break;
    case 'draft':
      if (m.shapes?.length) S.remoteDrafts[m.from] = m; else delete S.remoteDrafts[m.from];
      renderOverlay();
      break;
  }
}

const presentState = () => ({ kind: S.seq.kind, vids: S.seq.vids, shot: S.seq.shots ? segFor(S.anchor).entity_id : null,
  anchor: S.anchor, i: S.seq.i, frame: S.frame, playing: S.playing, rate: S.rate, cutFilter: S.cutFilter });

async function applyPresent(st) {
  if (!st?.vids?.length) return;
  S.cutFilter = st.cutFilter ?? null;
  if (st.vids.join() !== S.seq.vids.join()) {
    let shots = null, ep = null;
    if (st.shot) {
      try {
        const cut = await loadCut(st.shot);
        shots = st.vids.map(v => cut.shots.find(s => s.versions.some(x => x.version_id === v)));
        if (shots.some(s => !s)) shots = null; else ep = cut.episode.id;
      } catch (e) { /* plays without the compositor */ }
    }
    S.anchor = st.anchor; S.mode = ['mini', 'cut'].includes(st.kind) ? st.kind : 'one';
    setSeq(st.kind, [...st.vids], st.i, st.frame, { shots, ep });
  } else if (st.i !== S.seq.i) goSeg(st.i, st.frame);
  if (S.rate !== st.rate) { S.rate = st.rate; applyAudio(); $('#speed').value = String(st.rate); }
  if (st.playing) {
    if (video.paused) { seek(st.frame, { broadcast: false }); video.play().catch(() => toast('Click anywhere to follow playback')); }
    else if (Math.abs(S.frame - st.frame) > 3) video.currentTime = (st.frame + 0.5) / fps();
  } else {
    if (!video.paused) video.pause();
    seek(st.frame, { broadcast: false });
  }
}

// any local transport action while following = stop following (re-follow from the banner)
function local() {
  if (!S.following) return;
  S.optedOut = S.following;
  S.following = null;
  renderPresence(); placePointer();
}

function broadcastPresent() { if (S.presenting && S.cur) wsSend({ type: 'present', state: presentState() }); }
let lastPresent = 0, presentTimer = null;
function broadcastPresentThrottled() {
  if (!S.presenting) return;
  clearTimeout(presentTimer);
  const now = Date.now();
  if (now - lastPresent > 80) { lastPresent = now; broadcastPresent(); } else presentTimer = setTimeout(broadcastPresent, 90);
}
setInterval(() => { if (S.presenting && S.playing) broadcastPresent(); }, 1000);
function broadcastDraft() {
  if (S.presenting && S.cur) wsSend({ type: 'draft', version_id: S.cur.version_id, frame: S.frame, shapes: frameDraft() });
}

// Share: the studio link, or share links for one review. ShotGrid users get their group's rights on them;
// guests only get in when the link allows it, with the rights ticked here and its password, if any
const GUEST_ROWS = [['comment', 'Comment'], ['attach', 'Attach files'], ['download', 'Download'], ['versions', 'Other versions'], ['details', 'Details']];
const GUEST_DEFAULT = { comment: true, attach: false, download: false, versions: false, details: false };
const MAX_SHARE_DAYS = APP.share_max_days;
function linkFor(url) {
  const u = new URL(url), it = S.items.find(i => i.version_id === S.anchor);
  if (it) u.searchParams.set('item', it.id);
  return u.href;
}
const copyLink = url => navigator.clipboard.writeText(linkFor(url)).then(() => toast('Link copied'), () => prompt('Link', linkFor(url)));
const left = t => { const s = t - Date.now() / 1000; return s > 86400 ? `${Math.round(s / 86400)}d` : s > 3600 ? `${Math.round(s / 3600)}h` : `${Math.max(1, Math.round(s / 60))}m`; };
$('#btnShare').addEventListener('click', openShare);
async function openShare() {
  let ticks;
  try { ticks = { ...GUEST_DEFAULT, ...JSON.parse(localStorage.rt_share_guest || '{}') }; } catch (e) { ticks = GUEST_DEFAULT; }
  openModal(`<h2>Share</h2>
    ${S.review.studio_url ? '<div class="row"><b style="flex:1">Studio link</b><button id="btnCopyStudio">Copy</button></div>' : ''}
    <div class="row sh-sep"><label class="tick"><input type="checkbox" id="shGuests">Allow guests</label></div>
    <div id="shGuestOpts" hidden>
      <div class="row ticks">${GUEST_ROWS.map(([k, l]) => `<label class="tick"><input type="checkbox" data-k="${k}" id="shG_${k}" ${ticks[k] ? 'checked' : ''}>${l}</label>`).join('')}</div>
      <div class="row"><input type="password" id="shPass" placeholder="Password" autocomplete="new-password" style="flex:1"></div>
    </div>
    <div class="row"><span>Expires in</span><input type="number" id="shDays" min="1" max="${MAX_SHARE_DAYS}" value="${APP.share_default_days}"><span>days</span>
      <span style="flex:1"></span><button class="primary" id="btnNewShare">Create link</button></div>
    <div class="list" id="shList" hidden></div>
    <div class="foot"><button onclick="closeModal()">Close</button></div>`);
  if (S.review.studio_url) $('#btnCopyStudio').onclick = () => copyLink(S.review.studio_url);
  const days = $('#shDays');
  $('#modal').addEventListener('keydown', e => { if (e.target.matches('input') && e.key !== 'Escape') e.stopPropagation(); });
  days.addEventListener('change', () => { days.value = clamp(Math.round(+days.value || APP.share_default_days), 1, MAX_SHARE_DAYS); });
  $('#shGuests').addEventListener('change', e => { $('#shGuestOpts').hidden = !e.target.checked; });
  const list = async () => {
    const rows = await api('/shares').catch(e => { toast(e.message, true); return []; });
    const el = $('#shList');
    if (!el) return;
    el.hidden = !rows.length;
    el.innerHTML = rows.map(r => {
      const who = !r.guests ? 'ShotGrid users' : `Guests: ${GUEST_ROWS.filter(([k]) => r.perms[k]).map(([, l]) => l).join(', ') || 'Watch'}${r.has_password ? ' · Password' : ''}`;
      return `<div class="opt share" data-id="${r.id}"><span class="mono muted">${left(r.expires_at)}</span>
        <span class="grow" title="${esc(`${who} · ${r.created_by_name || ''}`)}">${esc(who)}</span>
        <button data-copy="${esc(r.url)}">Copy</button><button data-revoke="${r.id}">Revoke</button></div>`;
    }).join('');
  };
  $('#shList').addEventListener('click', async e => {
    const b = e.target.closest('button');
    if (b?.dataset.copy) copyLink(b.dataset.copy);
    if (b?.dataset.revoke) { try { await api(`/shares/${b.dataset.revoke}`, { method: 'DELETE' }); list(); } catch (err) { toast(err.message, true); } }
  });
  $('#btnNewShare').onclick = async () => {
    const guests = $('#shGuests').checked, perms = {};
    for (const c of $$('#shGuestOpts input[type=checkbox]')) perms[c.dataset.k] = c.checked;
    if (guests) localStorage.rt_share_guest = JSON.stringify(perms);
    try {
      const r = await api('/shares', { method: 'POST', body: { days: clamp(Math.round(+days.value || APP.share_default_days), 1, MAX_SHARE_DAYS), guests, perms, password: guests ? $('#shPass').value : null } });
      copyLink(r.url); $('#shPass').value = ''; list();
    } catch (e) { toast(e.message, true); }
  };
  list();
}

// Pointers: while someone presents, everyone's cursor goes to the room in frame coordinates. Who you see is your own choice;
// the presenter you follow always shows, and anyone else only on the version you're watching, where the position means something.
S.remotePtrs = {};
S.showPointers = localStorage.rt_pointers !== '0';
const roomPresenting = () => S.presenting || S.presence.some(p => p.presenting);
let lastPtr = 0, ptrTimer = null;
function sendPointer(p) {
  clearTimeout(ptrTimer);
  const now = Date.now();
  if (now - lastPtr >= 40 || !p) { lastPtr = now; wsSend({ type: 'pointer', p, v: S.cur?.version_id }); } else ptrTimer = setTimeout(() => sendPointer(p), 40 - (now - lastPtr));
}
stage.addEventListener('pointermove', e => { if (roomPresenting()) sendPointer(toNorm(e).map(round4)); });
stage.addEventListener('pointerleave', () => { if (roomPresenting()) sendPointer(null); });
function placePointer() {
  const box = $('#pointers'), R = contentRect(), here = new Set(S.presence.map(u => u.cid)), shown = new Set();
  for (const [cid, m] of Object.entries(S.remotePtrs)) {
    if (!here.has(cid)) { delete S.remotePtrs[cid]; continue; }
    if (!m.p || !(S.following === cid || (S.showPointers && m.v && m.v === S.cur?.version_id))) continue;
    shown.add(cid);
    let el = box.querySelector(`[data-cid="${cid}"]`);
    if (!el) {
      el = $('#pointerTpl').content.firstElementChild.cloneNode(true);
      el.dataset.cid = cid;
      el.style.setProperty('--pc', `hsl(${hue(m.name)} 70% 50%)`);
      el.querySelector('span').textContent = m.name;
      box.append(el);
    }
    el.style.transform = `translate(${R.x + m.p[0] * R.w}px, ${R.y + m.p[1] * R.h}px)`;
  }
  for (const el of [...box.children]) if (!shown.has(el.dataset.cid)) el.remove();
}
$('#btnPointers').addEventListener('click', () => {
  S.showPointers = !S.showPointers;
  localStorage.rt_pointers = S.showPointers ? '1' : '0';
  renderPresence(); placePointer();
});

$('#btnPresent').addEventListener('click', async () => {
  if (!S.me && !await ensureIdentity()) return;
  S.presenting = !S.presenting;
  if (S.presenting) { S.following = null; S.optedOut = null; broadcastPresent(); broadcastDraft(); }
  else wsSend({ type: 'present_stop' });
  renderPresence();
});

function renderPresence() {
  $('#presence').innerHTML = S.presence.map(p => {
    const it = S.items.find(i => i.id === p.item_id);
    return avatar(p.name, p.presenting ? 'presenting' : '', p.id, `${p.name}${p.presenting ? ' · presenting' : ''}${it ? ` · ${it.version_code}` : ''}`);
  }).join('');
  $('#btnPresent').classList.toggle('active', S.presenting);
  $('#btnPointers').hidden = !roomPresenting();
  $('#btnPointers').classList.toggle('on', S.showPointers);
  $('#btnPresent').textContent = S.presenting ? 'Presenting' : 'Present';
  const b = $('#followBanner'), presenter = S.presence.find(p => p.presenting && p.cid !== S.cid);
  if (presenter) {
    const following = S.following === presenter.cid;
    b.hidden = false;
    b.classList.toggle('on', following);
    b.innerHTML = `${avatar(presenter.name, 'sm', presenter.id)} <button>${following ? 'Following' : 'Follow'}</button>`;
    b.querySelector('button').onclick = () => {
      if (following) { S.optedOut = S.following; S.following = null; } else { S.following = presenter.cid; S.optedOut = null; }
      renderPresence(); placePointer();
    };
  } else b.hidden = true;
}

// ─── tooltips: name, shortcut and what it does, shown on hover ──────────────
const TIPS = {
  'tool:none': ['Select', 'V', 'Click a drawing to select it. Drag to move it, drag a handle to resize. Your posted drawings open for editing.'],
  'tool:brush': ['Brush', 'B', 'Freehand stroke that gets thicker as you press harder with a pen tablet.'],
  'tool:pen': ['Marker', 'P', 'Freehand stroke with the same width all along.'],
  'tool:line': ['Line', 'N', 'Straight line. Drag either end afterwards to adjust it.'],
  'tool:arrow': ['Arrow', 'A', 'Arrow pointing to where you let go.'],
  'tool:rect': ['Rectangle', 'R', 'Hold Shift for a square.'],
  'tool:ellipse': ['Ellipse', 'C', 'Hold Shift for a circle.'],
  'tool:text': ['Text', 'T', 'Click on the frame and type. Double-click text to change it.'],
  'tool:eraser': ['Eraser', 'E', 'Rubs out the part of a stroke it touches. Lines, shapes and text go whole. The size buttons set its width.'],
  color: ['Colour', '1–9', 'Colour for new drawings, or for the selected one.'],
  slot: ['Custom colour', '', 'Click to use it. Double-click to change it.'],
  slotEmpty: ['Custom colour', '', 'Click to save a colour of your own here.'],
  picker: ['Any colour', '', 'Open the colour picker.'],
  sizeRange: ['Size', '', 'Stroke width, text size or eraser width, for new drawings or the selected one.'],
  opacityRange: ['Opacity', '', 'How solid new drawings are, or the selected one. Published frames match.'],
  btnUndo: ['Undo', 'Ctrl+Z', 'Undo the last change to the drawing on this frame.'],
  btnRedo: ['Redo', 'Ctrl+Shift+Z', ''],
  btnClear: ['Clear frame', '', 'Remove the unposted drawing on this frame.'],
  btnOnion: ['Onion skin', 'U', 'Show drawings from nearby frames: red before, green after.'],
  onionN: ['Onion skin frames', '', 'How many frames before and after to show.'],
  btnHold: ['Hold', 'G', 'Each drawing stays on screen until the next one, posted or not. New notes last until the next drawing.'],
  btnDrawings: ['Show drawings', 'H', 'Hide every drawing to see the clean frame.'],
  btnZoom: ['Zoom', '', 'Fit to the screen, width or height, or pick a size. At 100% one movie pixel is one screen pixel. The mouse wheel zooms, the middle button pans, 0 fits the screen.'],
  btnFirst: ['First frame', 'Home', ''], btnLast: ['Last frame', 'End', ''],
  btnPrevNote: ['Previous note', '↑', 'Jump to the previous note or unposted drawing.'], btnNextNote: ['Next note', '↓', 'Jump to the next note or unposted drawing.'],
  btnBack: ['Previous frame', '←', 'Shift steps 10 frames.'], btnFwd: ['Next frame', '→', 'Shift steps 10 frames.'],
  btnPlay: ['Play / pause', 'Space', 'Plays inside In/Out when it is set.'],
  frameInput: ['Frame', '', 'Type a frame number and press Enter.'],
  speed: ['Speed', '', ''],
  btnLoop: ['Loop', '', 'Repeat the clip, the sequence or the In/Out range.'],
  btnMute: ['Mute', 'M', ''], volume: ['Volume', '', ''],
  btnExport: ['Download', '', 'This frame, the movie, or an export: PDF report, annotated frames as ZIP, notes as CSV.'],
  tc: ['Timecode / frames', '', 'Switch how frames are shown and typed.'],
  btnIn: ['Set In', 'I', 'Playback starts here.'], btnOut: ['Set Out', 'O', 'Playback stops here.'],
  btnClearIO: ['Clear In / Out', 'Esc', 'Play the whole sequence again.'],
  'rtab:notes': ['Notes', '', 'Notes on the version on screen.'],
  'rtab:shot': ['Shot notes', '', 'Every note on this shot, across its versions. Click one to open that version.'],
  'rtab:details': ['Details', '', 'ShotGrid fields of the version on screen.'],
  btnFields: ['Choose fields', '', 'Add or remove fields. Your list is kept in this browser.'],
  leftGrip: ['Resize', '', 'Drag to resize. Double-click to reset.'], rightGrip: ['Resize', '', 'Drag to resize. Double-click to reset.'],
  btnCopyStudio: ['Studio link', '', 'For the studio: opens after signing in with ShotGrid, with the rights of their group.'],
  btnNewShare: ['Create link', '', 'Makes a new link and copies it. It stops working when it expires.'],
  shDays: ['Expires in', '', `1 to ${APP.share_max_days} days.`],
  btnFull: ['Fullscreen', 'F', ''],
  btnAdd: ['Add versions', '', 'Search the project and add versions to this review.'],
  btnSort: ['Sort versions', '', 'Sort this list by a Version field or a field of what it links to. Only your view changes.'],
  btnShare: ['Share', '', 'Copy the studio link, or create a link to this review that expires.'],
  btnTour: ['Guided tour', '', 'A short walk through the tool.'],
  shGuests: ['Allow guests', '', 'Off: only people signed in with ShotGrid can open the link, with their group\'s rights. On: anyone with the link, with the rights ticked below.'],
  shPass: ['Guest password', '', 'Optional. Guests need it as well as the link, in case the link leaks. It can\'t be shown again.'],
  shG_comment: ['Comment', '', 'Guests can post notes and drawings.'],
  shG_attach: ['Attach files', '', `Guests can add files to their notes, up to ${APP.max_guest_upload_mb} MB each. They go to ShotGrid when the notes are published.`],
  btnAttach: ['Attach files', '', 'Add files to this note, or drop them on the panel or paste a picture. They go to ShotGrid with it.'],
  shG_download: ['Download', '', 'Guests can save the movie and frames. Off still streams the video.'],
  shG_versions: ['Other versions', '', 'Guests can use Mini-cut, the whole cut and Compare. Off: only the versions in this review.'],
  shG_details: ['Details', '', 'Guests can read basic ShotGrid fields of the version.'],
  btnPresent: ['Present', '', 'Everyone in the review follows your playback, frame by frame, and sees your pointer.'],
  btnPointers: ['Pointers', '', 'Show or hide the pointers of the others on this version. Only for you. The presenter you follow always shows.'],
  btnPublish: ['Publish to SG', '', 'Send unpublished notes to ShotGrid as Notes, with the annotated frames attached.'],
  statusBtn: ['Status', '', 'Changes the version status in ShotGrid straight away.'],
  'mode:one': ['Version', '', 'Play this version on its own.'],
  'mode:mini': ['Mini-cut', '', 'Play it between the previous and the next shot of the cut.'],
  'mode:cut': ['Cut', '', 'Play every shot in cut order. The rows above the timeline switch task per shot.'],
  btnCompare: ['Compare', '', 'Show another version of this shot next to it. It follows you from shot to shot.'],
  btnExitMulti: ['Clear selection', '', ''],
  'panel:left': ['Versions', '\\', 'Show or hide the versions list.'], 'panel:right': ['Right panel', '\\', 'Show or hide notes and details.'],
  'panel:top': ['Header', '\\', 'Show or hide the top bar.'], 'panel:bottom': ['Drawing tools', '\\', 'Show or hide the drawing tools.'],
  'cmode:side': ['Side by side', '', ''], 'cmode:wipe': ['Wipe', '', 'Drag the line to reveal one or the other.'],
  cmpRot: ['Rotate wipe', '', 'Turns the line 45°. Or drag its round handle: Shift snaps to 15°, double-click resets.'],
  'cmode:blend': ['Blend', '', 'Fade between the two with the slider.'], 'cmode:diff': ['Difference', '', 'Black where nothing changed.'],
  holdSelect: ['Duration', '', 'How many frames the note covers. Or click a drawing on the timeline, then shift-click where it should end. In → Out uses the yellow range (I and O keys).'],
  frameChip: ['Frames', '', 'Where the note goes. Click to jump there.'],
  btnPost: ['Post', 'Ctrl+Enter', 'Post the comment and every unposted drawing on this version, one note per drawn frame.'],
  noteFilter: ['Filter notes', '', ''],
};
function tipFor(el) {
  const d = el.dataset;
  const key = d.rtab ? `rtab:${d.rtab}` : d.tool ? `tool:${d.tool}` : d.mode ? `mode:${d.mode}` : d.panel ? `panel:${d.panel}` : d.cmode ? `cmode:${d.cmode}`
    : d.color ? 'color' : d.slot ? (S.slots[+d.slot] ? 'slot' : 'slotEmpty') : d.picker ? 'picker' : el.id;
  if (key === 'color') return ['Colour', String(COLORS.indexOf(d.color) + 1), TIPS.color[2]];
  if (TIPS[key]) return TIPS[key];
  if (el.title) { el.dataset.title = el.title; el.removeAttribute('title'); }  // plain tips use the same look
  return el.dataset.title ? [el.dataset.title, '', ''] : null;
}
(function tooltips() {
  const tip = $('#tip');
  let timer = null, shown = 0, cur = null;
  const hide = () => { clearTimeout(timer); if (!tip.hidden) shown = Date.now(); tip.hidden = true; cur = null; };
  document.addEventListener('mouseover', e => {
    const el = e.target.closest('button, select, input, a, label, .lbl, .cell, .mk, .chip, .dchip, .viewers .av, #presence .av, .seg, .grip');
    if (el === cur) return;
    hide();
    const t = el && tipFor(el);
    if (!t) return;
    cur = el;
    if (el.title) { el.dataset.title = el.title; el.removeAttribute('title'); }  // ours replaces the native one
    timer = setTimeout(() => {
      if (cur !== el || !el.isConnected) return;
      const [name, key, desc] = t;
      tip.innerHTML = `<div class="tt"><b>${esc(name)}</b>${key ? `<kbd>${esc(key)}</kbd>` : ''}</div>${desc ? `<div class="td">${esc(desc)}</div>` : ''}`;
      tip.hidden = false;
      const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
      const top = r.top - h - 8 > 4 ? r.top - h - 8 : r.bottom + 8;
      Object.assign(tip.style, { left: `${clamp(r.left + r.width / 2 - w / 2, 6, innerWidth - w - 6)}px`, top: `${top}px` });
    }, Date.now() - shown < 400 ? 0 : 450);  // once one tip has shown, neighbours show at once
  });
  document.addEventListener('mouseout', e => { if (cur && !cur.contains(e.relatedTarget)) hide(); });
  document.addEventListener('pointerdown', hide, true);
  document.addEventListener('keydown', hide, true);
})();

// ─── misc ───────────────────────────────────────────────────────────────────
function updateURL() {
  const u = new URL(location.href);
  for (const k of ['publish', 'note', 'frame', 'item', 'mode']) u.searchParams.delete(k);
  const it = S.items.find(i => i.version_id === S.anchor);
  if (it) u.searchParams.set('item', it.id);
  if (S.mode !== 'one') u.searchParams.set('mode', S.mode);
  history.replaceState(null, '', u);
}

function linkGone(msg) {
  S.modalLocked = true;
  $('#reviewName').textContent = msg;
  for (const v of [...VIDS, vC]) v.pause();
  openModal(`<h2>${esc(msg)}</h2>`);
}
// Shown when this address can't be opened yet: ShotGrid sign-in, and the guest door if the link has one
async function signInGate() {
  const g = await api(`/api/gate/${encodeURIComponent(TOKEN)}`).catch(() => ({}));
  return new Promise(resolve => {
    S.modalLocked = true;
    openModal(`<h2>Sign in</h2><button class="primary big" id="btnSG">Sign in with ShotGrid</button>
      ${g.password ? `<div class="or">or</div>
        <div class="row"><input type="text" id="guestName" placeholder="Your name" style="flex:1"></div>
        <div class="row"><input type="password" id="guestPass" placeholder="Password" autocomplete="off" style="flex:1"><button id="btnGuest">Join as guest</button></div>` : ''}`);
    const done = () => { S.modalLocked = false; closeModal(); resolve(); };
    $('#btnSG').addEventListener('click', async e => {
      const b = e.currentTarget;
      b.disabled = true; b.textContent = 'Waiting for ShotGrid…';
      try { S.me = await signInWithShotGrid(); done(); }
      catch (err) { toast(err.message, true); b.disabled = false; b.textContent = 'Sign in with ShotGrid'; }
    });
    if (!g.password) return;
    const join = async () => {
      const guest_name = $('#guestName').value.trim(), password = $('#guestPass').value;
      if (!guest_name) return $('#guestName').focus();
      try { S.me = (await api('/api/identify', { method: 'POST', body: { token: TOKEN, guest_name, password } })).user; done(); }
      catch (err) { toast(err.message, true); $('#guestPass').select(); }
    };
    $('#btnGuest').addEventListener('click', join);
    $('#modal').addEventListener('keydown', e => { e.stopPropagation(); if (e.key === 'Enter') join(); });
  });
}

function renderAll() {
  renderSidebar(); renderHeader(); renderStageMsg(); renderNotes(); renderTimeline(); renderComp(); renderComposer();
  renderPresence(); renderUser(); renderCompareBar(); layout(); onFrame();
}

window.addEventListener('beforeunload', e => { if (S.outbox.length) { e.preventDefault(); e.returnValue = ''; } });
window.addEventListener('online', flushOutbox);

(async function init() {
  renderToolbar();
  applyPanels();
  $('#btnLoop').classList.toggle('active', S.loop);
  fillRange($('#volume'));
  let d;
  try { d = await api(''); } catch (e) {
    if (e.status !== 401) return linkGone(e.message);
    await signInGate();
    try { d = await api(''); } catch (err) { return linkGone(err.message); }
  }
  applyData(d);
  migrateLegacyDrafts();
  await pullStore();
  api('/statuses').then(s => { S.statuses = s; S.items = sortItems(S.items); renderSidebar(); renderHeader(); renderTimeline(); renderComp(); }).catch(() => {});
  const noteId = +params.get('note') || null;
  const deepNote = noteId && S.notes.find(n => n.id === noteId);
  const it = S.items.find(i => i.id === (deepNote?.item_id || +params.get('item'))) || S.items[0];
  const frame = deepNote ? deepNote.frame : params.has('frame') ? +params.get('frame') : undefined;
  await openMode(can('versions') && params.get('mode') || 'one', it?.version_id ?? null, { frame, note: deepNote?.id });
  if (S.me) connectWS(); else await ensureIdentity();  // ensureIdentity connects on success
  flushOutbox();
  if (params.get('publish')) openPublish();
  if (params.get('export') && can('download')) openExport();
})();
