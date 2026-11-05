#!/usr/bin/env python3
"""
Server-side annotation renderer (Pillow). Mirrors drawShape() in static/draw.js:
coordinates are normalized to the video frame (0..1), stroke widths and text sizes are
fractions of the frame height. Drawn at 2x and downsampled for anti-aliasing.
"""
import math
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, ImageDraw, ImageFont, ImageColor

from . import media
from .config import FONT_PATH, RENDER_WORKERS

SS = 2  # supersampling factor
POOL = ThreadPoolExecutor(RENDER_WORKERS, thread_name_prefix='render')  # ffmpeg and Pillow work outside the GIL, so frames render side by side


def _rgba(color, alpha=255):
    try:
        r, g, b = ImageColor.getrgb(color)[:3]
    except ValueError:
        r, g, b = 255, 0, 0
    return (r, g, b, alpha)


def _font(px):
    try:
        return ImageFont.truetype(FONT_PATH, max(6, int(px)))
    except OSError:
        return ImageFont.load_default()


def _line(d, pts, color, width):
    width = max(1, int(round(width)))
    d.line(pts, fill=color, width=width, joint='curve')
    r = width / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)


def draw_shape(d, s, W, H):
    color = _rgba(s.get('c', '#ff3b30'))
    w = float(s.get('w', 0.006)) * H
    P = lambda p: (p[0] * W, p[1] * H)
    t = s.get('t')
    if t == 'pen':
        pts = s.get('pts') or []
        if not pts:
            return
        if len(pts) == 1:
            x, y = P(pts[0])
            r = w * _pw(pts[0]) / 2
            d.ellipse((x - r, y - r, x + r, y + r), fill=color)
            return
        _stroke(d, _samples([(*P(p), w * _pw(p)) for p in pts]), color)
    elif t in ('line', 'arrow'):
        a, b = P(s['a']), P(s['b'])
        _line(d, [a, b], color, w)
        if t == 'arrow':
            ang = math.atan2(b[1] - a[1], b[0] - a[0])
            head = max(w * 4, H * 0.02)
            for sign in (1, -1):
                hx = b[0] - head * math.cos(ang + sign * math.pi / 7)
                hy = b[1] - head * math.sin(ang + sign * math.pi / 7)
                _line(d, [b, (hx, hy)], color, w)
    elif t in ('rect', 'ellipse'):
        (x1, y1), (x2, y2) = P(s['a']), P(s['b'])
        box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
        if t == 'rect':
            d.rectangle(box, outline=color, width=max(1, int(round(w))))
        else:
            d.ellipse(box, outline=color, width=max(1, int(round(w))))
    elif t == 'text':
        size = float(s.get('s', 0.04)) * H
        x, y = P(s['a'])
        d.multiline_text((x, y), s.get('txt', ''), font=_font(size), fill=color,
                         stroke_width=max(1, int(size / 12)), stroke_fill=(0, 0, 0, 200))


def _samples(q):
    """Same smoothing as strokeSamples() in draw.js: quadratic curves through the sample midpoints."""
    n = len(q)
    q = [(p[0], p[1], (q[max(0, i - 1)][2] + 2 * p[2] + q[min(n - 1, i + 1)][2]) / 4) for i, p in enumerate(q)]
    if n < 3:
        return q
    out, a = [q[0]], q[0]
    for i in range(1, n - 1):
        c = q[i]
        b = tuple((u + v) / 2 for u, v in zip(q[i], q[i + 1]))
        k = max(1, math.ceil((math.dist(a[:2], c[:2]) + math.dist(c[:2], b[:2])) / (1.5 * SS)))
        for j in range(1, k + 1):
            t = j / k
            out.append(tuple((1 - t) ** 2 * x + 2 * (1 - t) * t * y + t * t * z for x, y, z in zip(a, c, b)))
        a = b
    out.append(q[-1])
    return out


def _stroke(d, smp, color):
    """A round brush along the samples: a disc at each one, a tapered strip to the next. Pillow's own wide
    lines leave teeth along the edges when the segments are this short."""
    prev = None
    for x, y, w in smp:
        r = max(1.0, w / 2)
        d.ellipse((x - r, y - r, x + r, y + r), fill=color)
        if prev:
            px, py, pr = prev
            length = math.hypot(x - px, y - py)
            if length:
                nx, ny = -(y - py) / length, (x - px) / length
                d.polygon([(px + nx * pr, py + ny * pr), (x + nx * r, y + ny * r),
                           (x - nx * r, y - ny * r), (px - nx * pr, py - ny * pr)], fill=color)
        prev = (x, y, r)


def _pw(p):
    """Pressure multiplier, same curve as the client."""
    return 0.35 + 1.3 * p[2] if len(p) > 2 else 1.0


def _opacity(s):
    try:
        return max(0.0, min(1.0, float(s.get('o', 1))))
    except (TypeError, ValueError):
        return 1.0


def _downsample(layer, size):
    """Back to frame size, working only on the part that has drawings (usually a small corner of a big image)."""
    out = Image.new('RGBA', size, (0, 0, 0, 0))
    box = layer.getbbox()
    if not box:
        return out
    pad = 8 * SS
    x0, y0 = max(0, box[0] - pad) // SS * SS, max(0, box[1] - pad) // SS * SS
    x1, y1 = min(size[0], math.ceil((box[2] + pad) / SS)) * SS, min(size[1], math.ceil((box[3] + pad) / SS)) * SS
    out.paste(layer.crop((x0, y0, x1, y1)).resize(((x1 - x0) // SS, (y1 - y0) // SS), Image.LANCZOS), (x0 // SS, y0 // SS))
    return out


def overlay(size, shapes):
    W, H = size[0] * SS, size[1] * SS
    layer = Image.new('RGBA', (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for s in shapes or []:
        o = _opacity(s)
        if o >= 1:
            draw_shape(d, s, W, H)
        else:  # its own flat layer, same as the client
            own = Image.new('RGBA', (W, H), (0, 0, 0, 0))
            draw_shape(ImageDraw.Draw(own), s, W, H)
            box = own.getbbox()
            if box:
                part = own.crop(box)
                part.putalpha(part.getchannel('A').point(lambda v: int(v * o)))
                layer.alpha_composite(part, dest=box[:2])
    return _downsample(layer, size)


def caption(img, text):
    layer = Image.new('RGBA', img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    px = max(12, img.height // 45)
    font = _font(px)
    pad = px // 2
    bbox = d.textbbox((0, 0), text, font=font)
    h = bbox[3] - bbox[1] + pad * 2
    d.rectangle((0, img.height - h, bbox[2] + pad * 2, img.height), fill=(0, 0, 0, 170))
    d.text((pad, img.height - h + pad - bbox[1]), text, font=font, fill=(255, 255, 255, 255))
    img.alpha_composite(layer)


def note_image(item, note, with_caption=True):
    """The note's frame with its drawing composited on top, as an RGB image."""
    fd, path = tempfile.mkstemp(suffix='.png', prefix='review_frame_')
    os.close(fd)
    try:
        media.extract_frame(item['version_id'], note['frame'], path)
        base = Image.open(path).convert('RGBA')
    finally:
        os.remove(path)
    base.alpha_composite(overlay(base.size, note.get('shapes')))
    if with_caption:
        frame = item['first_frame'] + note['frame']
        caption(base, f"{item['version_code']}  ·  f{frame}  ·  {note['author_name']}")
    return base.convert('RGB')


def render_note_frame(item, note, with_caption=True):
    """Return path to a PNG of the note's frame with its drawing composited on top."""
    img = note_image(item, note, with_caption)
    fd, path = tempfile.mkstemp(suffix='.png', prefix='review_frame_')
    os.close(fd)
    img.save(path, 'PNG', compress_level=1)
    return path


def render_later(item, note, with_caption=True):
    """Start rendering now; the Future gives the PNG's path."""
    return POOL.submit(render_note_frame, item, note, with_caption)
