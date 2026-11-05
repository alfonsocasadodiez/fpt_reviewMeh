#!/usr/bin/env python3
"""
Exports of a review's posted notes: CSV, annotated frames as a ZIP, PDF report.

They run as background jobs (a big ZIP can outlast a proxy's timeout) and write to the NAS. Finished files
are kept for an hour so a download can be retried, then removed.
"""
import csv
import html
import logging
import os
import re
import secrets
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from PIL import Image

from . import db
from . import media
from . import render
from . import sg_bridge
from .config import MEDIA_ROOT, LOGO_PATH, SG_URL, ACCENT

logger = logging.getLogger(__name__)

EXPORT_DIR = os.path.join(MEDIA_ROOT, 'exports')
KEEP = 3600
KINDS = {'csv': ('notes', 'csv'), 'zip': ('frames', 'zip'), 'pdf': ('report', 'pdf')}
_jobs = {}
_lock = threading.Lock()
_pool = ThreadPoolExecutor(2, thread_name_prefix='export')


def prune():
    now = time.time()
    with _lock:
        for k in [k for k, j in _jobs.items() if j['status'] != 'running' and now - j['at'] > KEEP]:
            shutil.rmtree(_jobs.pop(k)['dir'], ignore_errors=True)


def clear_leftovers():
    """Jobs live in memory, so anything on disk at start belongs to a previous run."""
    shutil.rmtree(EXPORT_DIR, ignore_errors=True)


def start(kind, review, item_ids, links):
    """links: add links to ShotGrid (people outside the studio can't open them)."""
    prune()
    jid = secrets.token_urlsafe(24)
    work = os.path.join(EXPORT_DIR, jid)
    os.makedirs(work, exist_ok=True)
    stem, ext = KINDS[kind]
    name = re.sub(r'[^\w.-]+', '_', review.get('playlist_name') or review['name']).strip('_') or 'review'
    job = {'id': jid, 'room': review['id'], 'status': 'running', 'done': 0, 'total': 0, 'error': None, 'dir': work, 'at': time.time(),
           'path': os.path.join(work, f'{name}_{stem}.{ext}'), 'filename': f'{name}_{stem}.{ext}'}
    with _lock:
        _jobs[jid] = job
    _pool.submit(_run, job, kind, review, set(item_ids), links)
    return jid


def get(jid, room):
    job = _jobs.get(jid)
    return job if job and job['room'] == room else None


def _run(job, kind, review, item_ids, links):
    try:
        if kind != 'csv':  # the frames come from the proxies, which may have been cleared or be rebuilding
            media.wait_ready([it['version_id'] for it in db.review_items(review['id']) if not item_ids or it['id'] in item_ids])
        data = _collect(review, item_ids)
        {'csv': _write_csv, 'zip': _write_zip, 'pdf': _write_pdf}[kind](job, review, data, links)
        job['status'] = 'done'
    except Exception as e:
        logger.exception('Export %s failed for review %s', kind, review['id'])
        job.update(status='error', error=str(e)[:200])
    job['at'] = time.time()


def _collect(review, item_ids):
    """[(item, top-level notes, {note id: replies})], in review order."""
    out = []
    for it in db.review_items(review['id']):
        if item_ids and it['id'] not in item_ids:
            continue
        notes = db.item_notes(it['id'])
        tops = [n for n in notes if not n['parent_id']]
        replies = {}
        for r in sorted((n for n in notes if n['parent_id']), key=lambda n: n['created_at']):
            replies.setdefault(r['parent_id'], []).append(r)
        if tops:
            out.append((it, tops, replies, notes))
    return out


def _span(n, it, notes):
    """(frame label, timecode) of a note, the way ShotGrid notes write them."""
    label, _, tc = sg_bridge.frame_label(n, it, it['fps'], sg_bridge.note_end(n, notes, it['frame_count']))
    return label, tc


def _when(t):
    return datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M') if t else ''


def _cell(v):
    """Text from a note can't run as a spreadsheet formula."""
    v = '' if v is None else str(v)
    return f"'{v}" if v[:1] in ('=', '+', '-', '@', '\t', '\r') else v


def _write_csv(job, review, data, links):
    job['total'] = sum(len(d[1]) for d in data)
    with open(job['path'], 'w', newline='', encoding='utf-8-sig') as f:  # the BOM makes Excel read accents correctly
        w = csv.writer(f)
        w.writerow(['Version', 'Entity', 'Task', 'Artist', 'Frames', 'Timecode', 'Type', 'Author', 'Text', 'Drawing', 'Attachments', 'Resolved', 'In ShotGrid', 'Created'])
        for it, tops, replies, notes in data:
            base = [it['version_code'], it['entity_name'], it['task_name'], it['artist_name']]
            for n in tops:
                label, tc = _span(n, it, notes)
                w.writerow([_cell(v) for v in base + [label, tc, 'Note', n['author_name'], n['text'], 'Yes' if n['shapes'] else '', ', '.join(f['name'] for f in n['files']),
                                                      'Yes' if n['resolved'] else '', 'Yes' if n['sg_note_id'] else '', _when(n['created_at'])]])
                for r in replies.get(n['id'], []):
                    w.writerow([_cell(v) for v in base + [label, tc, 'Reply', r['author_name'], r['text'], '', ', '.join(f['name'] for f in r['files']), '', 'Yes' if r['sg_reply_id'] else '', _when(r['created_at'])]])
                job['done'] += 1


def _write_zip(job, review, data, links):
    drawn = [(it, n) for it, tops, _, _ in data for n in tops if n['shapes'] and it['media_status'] == 'ready']
    job['total'] = len(drawn)
    if not drawn:
        raise ValueError('No drawings to export')
    renders = [render.render_later(it, n) for it, n in drawn]
    with zipfile.ZipFile(job['path'], 'w', zipfile.ZIP_STORED) as z:  # PNGs are already compressed
        for (it, n), fut in zip(drawn, renders):
            path = fut.result()
            try:
                z.write(path, f"{it['version_code']}/{it['version_code']}.{it['first_frame'] + n['frame']}_note{n['id']}.png")
            finally:
                os.remove(path)
            job['done'] += 1


PDF_CSS = """
@page { size: A4; margin: 14mm 14mm 16mm;
  @bottom-right { content: counter(page) " / " counter(pages); font-size: 8pt; color: #889; } }
* { box-sizing: border-box; }
body { font-family: 'DejaVu Sans', Arial, sans-serif; font-size: 9pt; line-height: 1.4; color: #1f2933; margin: 0; }
a { color: __ACCENT__; text-decoration: none; }
header { background: linear-gradient(135deg, #1a1a2e 0%, __ACCENT__ 100%); color: #fff; padding: 16px 20px; border-radius: 8px; margin-bottom: 14px; }
header table { width: 100%; border-collapse: collapse; }
header td { padding: 0; vertical-align: middle; }
header .logo { width: 56px; } header img { height: 44px; display: block; }
header h1 { margin: 0; font-size: 16pt; font-weight: 600; }
header .meta { font-size: 8pt; opacity: .9; margin-top: 4px; }
.sum { width: 100%; border-collapse: collapse; margin-bottom: 8px; font-size: 8pt; }
.sum th { text-align: left; color: #667; font-weight: 600; border-bottom: 1px solid #cdd5df; padding: 4px 6px; }
.sum td { border-bottom: 1px solid #e6eaf0; padding: 4px 6px; }
.sum .n { text-align: right; width: 56px; } .sum th.n { text-align: right; }
.sum a { color: inherit; } .sum a.pg::after { content: target-counter(attr(href), page); }
.ver { margin-top: 14px; } .ver + .ver { break-before: page; margin-top: 0; }
.vh { border-bottom: 2px solid __ACCENT__; padding-bottom: 6px; margin-bottom: 10px; }
.vh h2 { margin: 0; font-size: 12pt; color: #12344d; overflow-wrap: anywhere; }
.vh .sub { font-size: 8pt; color: #667; margin-top: 2px; }
.dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; margin-right: 3px; }
.note { break-inside: avoid; border: 1px solid #dfe4ea; border-radius: 6px; margin-bottom: 8px; overflow: hidden; }
.note.done { opacity: .72; }
.note table { width: 100%; border-collapse: collapse; }
.note td { vertical-align: top; padding: 0; }
.note .img { width: 46%; }
.note .img img { width: 100%; display: block; }
.note .body { padding: 8px 10px; }
.head { font-size: 8pt; color: #667; margin-bottom: 4px; }
.head b { color: #1f2933; }
.fr { display: inline-block; background: #e6f4fb; color: #0875a5; border-radius: 8px; padding: 0 6px; font-weight: 600; margin-right: 4px; }
.txt { white-space: pre-wrap; overflow-wrap: anywhere; }
.rep { margin-top: 5px; padding-left: 7px; border-left: 2px solid #cdd5df; font-size: 8pt; white-space: pre-wrap; overflow-wrap: anywhere; }
.rep b { display: block; color: #445; }
.att { display: block; max-width: 100%; max-height: 42mm; margin-top: 5px; border-radius: 3px; }
.attn { margin-top: 4px; font-size: 8pt; color: #0875a5; }
.tag { color: #2d8a4e; font-weight: 600; margin-left: 4px; }
"""


PDF_CSS = PDF_CSS.replace('__ACCENT__', ACCENT)


def _write_pdf(job, review, data, links):
    try:
        from weasyprint import HTML  # slow to import, and only reports need it
    except ImportError:
        raise ValueError('PDF reports need: pip install "reviewmeh[pdf]"')
    e = html.escape
    job['total'] = sum(len(d[1]) for d in data)
    if not job['total']:
        raise ValueError('No notes to export')
    statuses = {s['code']: s for s in sg_bridge.version_statuses(review['project_id'])}
    title = review.get('playlist_name') or review['name']
    parts, rows, total, open_notes = [], [], 0, 0
    jpegs = {n['id']: render.POOL.submit(_frame_jpeg, job, it, n) for it, tops, _, _ in data for n in tops}
    for i, (it, tops, replies, notes) in enumerate(data):
        st = statuses.get(it['sg_status']) or {'name': it['sg_status'] or '', 'color': '#889'}
        n_open = sum(1 for n in tops if not n['resolved'])
        total += len(tops)
        open_notes += n_open
        rows.append(f'<tr><td><a href="#v{i}">{e(it["version_code"])}</a></td><td>{e(it["task_name"] or "")}</td><td>{e(st["name"])}</td>'
                    f'<td class="n">{len(tops)}</td><td class="n">{n_open}</td><td class="n"><a class="pg" href="#v{i}"></a></td></tr>')
        sub = ' · '.join(e(x) for x in (it['entity_name'], it['task_name'], it['artist_name']) if x)
        ver_name = e(it['version_code'])
        if links:
            ver_name = f'<a href="{SG_URL}/detail/Version/{it["version_id"]}">{ver_name}</a>'
        parts.append(f'<section class="ver" id="v{i}"><div class="vh"><h2>{ver_name}</h2><div class="sub">{sub} · '
                     f'<span class="dot" style="background:{e(st["color"])}"></span>{e(st["name"])}</div></div>')
        for n in tops:
            label, tc = _span(n, it, notes)
            img = jpegs[n['id']].result()
            tags = '<span class="tag">Resolved</span>' if n['resolved'] else ''
            if n['sg_note_id'] and links:
                tags += f' <a href="{SG_URL}/detail/Note/{n["sg_note_id"]}">ShotGrid</a>'
            reps = ''.join(f'<div class="rep"><b>{e(r["author_name"])} · {_when(r["created_at"])}</b>{e(r["text"])}{_files_html(job, r)}</div>' for r in replies.get(n['id'], []))
            body = (f'<div class="head"><span class="fr">{e(label)}</span>{e(tc)}<br><b>{e(n["author_name"])}</b> · {_when(n["created_at"])}{tags}</div>'
                    f'<div class="txt">{e(n["text"] or "")}</div>{_files_html(job, n)}{reps}')
            cells = f'<td class="img"><img src="file://{img}"></td>' if img else ''
            parts.append(f'<div class="note{" done" if n["resolved"] else ""}"><table><tr>{cells}<td class="body">{body}</td></tr></table></div>')
            job['done'] += 1
        parts.append('</section>')
    meta = [e(review.get('project_name') or ''), datetime.now().strftime('%Y-%m-%d %H:%M'),
            f'{len(data)} version{"" if len(data) == 1 else "s"}', f'{total} note{"" if total == 1 else "s"}', f'{open_notes} unresolved']
    doc = (f'<!DOCTYPE html><html><head><meta charset="utf-8"><title>{e(title)}</title><style>{PDF_CSS}</style></head><body>'
           f'<header><table><tr><td class="logo"><img src="file://{LOGO_PATH}"></td><td><h1>{e(title)}</h1><div class="meta">{" · ".join(m for m in meta if m)}</div></td></tr></table></header>'
           f'<table class="sum"><tr><th>Version</th><th>Task</th><th>Status</th><th class="n">Notes</th><th class="n">Open</th><th class="n">Page</th></tr>{"".join(rows)}</table>'
           f'{"".join(parts)}</body></html>')
    HTML(string=doc, base_url=job['dir']).write_pdf(job['path'])


def _files_html(job, n):
    """Attached pictures small, other files by name."""
    out = []
    for f in n['files']:
        path = (db.file_row(f['id']) or {}).get('path')
        thumb = None
        if path and f['name'].lower().rsplit('.', 1)[-1] in ('png', 'jpg', 'jpeg'):
            try:
                im = Image.open(path).convert('RGB')
                im.thumbnail((700, 700))
                thumb = os.path.join(job['dir'], f"a{f['id']}.jpg")
                im.save(thumb, 'JPEG', quality=84)
            except Exception:
                logger.warning('Could not read attachment %s for a report', f['id'], exc_info=True)
                thumb = None
        out.append(f'<img class="att" src="file://{thumb}">' if thumb else f'<div class="attn">{html.escape(f["name"])}</div>')
    return ''.join(out)


def _frame_jpeg(job, it, n):
    """The note's frame with its drawing, small enough for a report; None if it can't be read."""
    if it['media_status'] != 'ready':
        return None
    try:
        out = os.path.join(job['dir'], f"f{n['id']}.jpg")
        im = render.note_image(it, n, False)
        im.thumbnail((1000, 1000))
        im.save(out, 'JPEG', quality=84)
        return out
    except Exception:
        logger.warning('Could not render note %s for a report', n['id'], exc_info=True)
        return None
