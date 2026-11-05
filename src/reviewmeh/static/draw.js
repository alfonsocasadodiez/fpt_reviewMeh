'use strict';
/* Vector shapes: pure geometry, no app state. render.py mirrors drawShape() for published PNGs.
 *
 * Shapes are normalized to the video frame: x,y in 0..1, stroke width `w` and text size `s` are
 * fractions of the frame height.
 *   pen   {t, c, w, pts: [[x, y, pressure?]]}   brush strokes carry pressure, marker strokes don't
 *   line | arrow | rect | ellipse   {t, c, w, a: [x, y], b: [x, y]}
 *   text  {t, c, s, a: [x, y], txt}
 * Any shape may carry `o`, its opacity (0..1, 1 when absent).
 * R is the frame's rectangle on screen {x, y, w, h} in CSS pixels.
 */

const pw = p => (p.length > 2 ? 0.35 + 1.3 * p[2] : 1);  // pressure → width multiplier (same curve as render.py)
const FONT = '"DejaVu Sans", "Segoe UI", sans-serif';

// Freehand smoothing (render.py does the same): the stroke runs as quadratic curves through the
// midpoints between samples, so it has no corners, and widths are eased over neighbours.
// q: [[x, y, width]] in pixels → dense samples about 1.5 px apart.
function strokeSamples(q) {
  q = q.map((p, i) => [p[0], p[1], (q[Math.max(0, i - 1)][2] + 2 * p[2] + q[Math.min(q.length - 1, i + 1)][2]) / 4]);
  if (q.length < 3) return q;
  const mid = (a, b) => a.map((v, k) => (v + b[k]) / 2), out = [q[0]];
  let a = q[0];
  for (let i = 1; i < q.length - 1; i++) {
    const ctl = q[i], b = mid(q[i], q[i + 1]);
    const n = Math.max(1, Math.ceil((Math.hypot(ctl[0] - a[0], ctl[1] - a[1]) + Math.hypot(b[0] - ctl[0], b[1] - ctl[1])) / 1.5));
    for (let j = 1; j <= n; j++) {
      const t = j / n, u = 1 - t;
      out.push(a.map((v, k) => u * u * v + 2 * u * t * ctl[k] + t * t * b[k]));
    }
    a = b;
  }
  out.push(q[q.length - 1]);
  return out;
}
// Consecutive samples of about the same width share one path (fewer seams, cheaper)
function strokeRuns(smp) {
  const runs = [];
  for (const p of smp) {
    const r = runs[runs.length - 1];
    if (r && Math.abs(p[2] - r.w) < 0.4) r.pts.push(p);
    else runs.push({ w: p[2], pts: r ? [r.pts[r.pts.length - 1], p] : [p] });
  }
  return runs.filter(r => r.pts.length > 1);
}

function drawShape(c, s, R, alpha = 1) {
  if (s.o != null && s.o < 1) {  // drawn flat first: a translucent stroke doesn't darken where it overlaps itself
    const off = drawShape.off ||= document.createElement('canvas');
    if (off.width !== c.canvas.width || off.height !== c.canvas.height) { off.width = c.canvas.width; off.height = c.canvas.height; }
    const x = off.getContext('2d');
    x.setTransform(1, 0, 0, 1, 0, 0); x.clearRect(0, 0, off.width, off.height); x.setTransform(c.getTransform());
    drawShape(x, { ...s, o: 1 }, R);
    c.save(); c.setTransform(1, 0, 0, 1, 0, 0); c.globalAlpha = alpha * s.o; c.drawImage(off, 0, 0); c.restore();
    return;
  }
  const P = p => [R.x + p[0] * R.w, R.y + p[1] * R.h];
  const w = (s.w || 0.006) * R.h;
  c.save();
  c.globalAlpha = alpha;
  c.strokeStyle = c.fillStyle = s.c || '#ff3b30';
  c.lineCap = 'round'; c.lineJoin = 'round';
  if (s.t === 'pen') {
    const pts = s.pts || [];
    if (pts.length === 1) {
      const [x, y] = P(pts[0]);
      c.beginPath(); c.arc(x, y, w * pw(pts[0]) / 2, 0, Math.PI * 2); c.fill();
    }
    for (const run of strokeRuns(strokeSamples(pts.map(p => [...P(p), w * pw(p)])))) {
      c.lineWidth = Math.max(1, run.w);
      c.beginPath(); c.moveTo(run.pts[0][0], run.pts[0][1]);
      for (let i = 1; i < run.pts.length; i++) c.lineTo(run.pts[i][0], run.pts[i][1]);
      c.stroke();
    }
  } else if (s.t === 'line' || s.t === 'arrow') {
    const [x1, y1] = P(s.a), [x2, y2] = P(s.b);
    c.lineWidth = Math.max(1, w);
    c.beginPath(); c.moveTo(x1, y1); c.lineTo(x2, y2);
    if (s.t === 'arrow') {
      const ang = Math.atan2(y2 - y1, x2 - x1), head = Math.max(w * 4, R.h * 0.02);
      for (const sg of [1, -1]) {
        c.moveTo(x2, y2);
        c.lineTo(x2 - head * Math.cos(ang + sg * Math.PI / 7), y2 - head * Math.sin(ang + sg * Math.PI / 7));
      }
    }
    c.stroke();
  } else if (s.t === 'rect' || s.t === 'ellipse') {
    const [x1, y1] = P(s.a), [x2, y2] = P(s.b);
    c.lineWidth = Math.max(1, w);
    c.beginPath();
    if (s.t === 'rect') c.rect(Math.min(x1, x2), Math.min(y1, y2), Math.abs(x2 - x1), Math.abs(y2 - y1));
    else c.ellipse((x1 + x2) / 2, (y1 + y2) / 2, Math.abs(x2 - x1) / 2, Math.abs(y2 - y1) / 2, 0, 0, Math.PI * 2);
    c.stroke();
  } else if (s.t === 'text') {
    const size = (s.s || 0.04) * R.h, [x, y] = P(s.a);
    c.font = `bold ${size}px ${FONT}`;
    c.textBaseline = 'top';
    c.lineWidth = Math.max(2, size / 6);
    c.strokeStyle = 'rgba(0,0,0,.8)';
    String(s.txt || '').split('\n').forEach((line, i) => {
      c.strokeText(line, x, y + i * size * 1.2);
      c.fillText(line, x, y + i * size * 1.2);
    });
  }
  c.restore();
}

// Screen-space bounding box {x, y, w, h}; c is a 2D context used to measure text
function shapeBox(c, s, R) {
  const P = p => [R.x + p[0] * R.w, R.y + p[1] * R.h];
  if (s.t === 'text') {
    const size = (s.s || 0.04) * R.h, [x, y] = P(s.a), lines = String(s.txt || '').split('\n');
    c.save(); c.font = `bold ${size}px ${FONT}`;
    const w = Math.max(...lines.map(l => c.measureText(l).width));
    c.restore();
    return { x, y, w, h: lines.length * size * 1.2 };
  }
  const pts = (s.t === 'pen' ? s.pts : [s.a, s.b]).map(P);
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x = Math.min(...xs), y = Math.min(...ys);
  return { x, y, w: Math.max(...xs) - x, h: Math.max(...ys) - y };
}

function distSeg(p, a, b) {
  const dx = b[0] - a[0], dy = b[1] - a[1], l = dx * dx + dy * dy;
  const t = l ? Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l)) : 0;
  return Math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy);
}

// p is normalized; hit-testing happens in screen pixels so the tolerance feels the same at any zoom
function hitShape(c, s, p, R) {
  const P = q => [R.x + q[0] * R.w, R.y + q[1] * R.h], q = P(p), tol = 8 + (s.w || 0.006) * R.h;
  if (s.t === 'pen') { const pts = s.pts.map(P); return pts.some((pt, i) => distSeg(q, i ? pts[i - 1] : pt, pt) < tol); }
  if (s.t === 'line' || s.t === 'arrow') return distSeg(q, P(s.a), P(s.b)) < tol;
  if (s.t === 'rect') {
    const [a, b] = [P(s.a), P(s.b)], k = [[a[0], a[1]], [b[0], a[1]], [b[0], b[1]], [a[0], b[1]]];
    return k.some((pt, i) => distSeg(q, pt, k[(i + 1) % 4]) < tol);
  }
  if (s.t === 'ellipse') {
    const [a, b] = [P(s.a), P(s.b)], cx = (a[0] + b[0]) / 2, cy = (a[1] + b[1]) / 2;
    const rx = Math.abs(b[0] - a[0]) / 2 || 1, ry = Math.abs(b[1] - a[1]) / 2 || 1;
    return Math.abs(Math.hypot((q[0] - cx) / rx, (q[1] - cy) / ry) - 1) * Math.min(rx, ry) < tol;
  }
  if (s.t === 'text') {
    const bx = shapeBox(c, s, R);
    return q[0] >= bx.x - 4 && q[0] <= bx.x + bx.w + 4 && q[1] >= bx.y - 4 && q[1] <= bx.y + bx.h + 4;
  }
  return false;
}

const r4 = v => Math.round(v * 1e4) / 1e4;

// New shape moved by (dx, dy) in normalized units
function moveShape(s, dx, dy) {
  const m = p => [r4(p[0] + dx), r4(p[1] + dy), ...p.slice(2)];
  if (s.t === 'pen') return { ...s, pts: s.pts.map(m) };
  return { ...s, a: m(s.a), ...(s.b ? { b: m(s.b) } : {}) };
}

// Resize handles in screen pixels: line ends, else the bounding box (corners only for text)
function shapeHandles(c, s, R) {
  if (s.t === 'line' || s.t === 'arrow') return [['a', R.x + s.a[0] * R.w, R.y + s.a[1] * R.h], ['b', R.x + s.b[0] * R.w, R.y + s.b[1] * R.h]];
  const b = shapeBox(c, s, R), x2 = b.x + b.w, y2 = b.y + b.h, mx = b.x + b.w / 2, my = b.y + b.h / 2;
  const corners = [['nw', b.x, b.y], ['ne', x2, b.y], ['se', x2, y2], ['sw', b.x, y2]];
  return s.t === 'text' ? corners : corners.concat([['n', mx, b.y], ['e', x2, my], ['s', mx, y2], ['w', b.x, my]]);
}

// Shape s with handle k dragged to normalized point p; `keep` holds the aspect ratio (always for text)
function resizeShape(c, s, R, k, p, keep) {
  if (k === 'a' || k === 'b') return { ...s, [k]: [r4(p[0]), r4(p[1])] };
  const sb = shapeBox(c, s, R), B = { x: (sb.x - R.x) / R.w, y: (sb.y - R.y) / R.h, w: sb.w / R.w, h: sb.h / R.h };
  let x1 = B.x, y1 = B.y, x2 = B.x + B.w, y2 = B.y + B.h;
  if (k.includes('w')) x1 = p[0];
  if (k.includes('e')) x2 = p[0];
  if (k.includes('n')) y1 = p[1];
  if (k.includes('s')) y2 = p[1];
  if ((keep || s.t === 'text') && k.length === 2) {
    const z = Math.max(0.05, B.w ? (x2 - x1) / B.w : 1, B.h ? (y2 - y1) / B.h : 1);
    if (k.includes('w')) x1 = x2 - B.w * z; else x2 = x1 + B.w * z;
    if (k.includes('n')) y1 = y2 - B.h * z; else y2 = y1 + B.h * z;
  }
  const sx = B.w ? (x2 - x1) / B.w : 1, sy = B.h ? (y2 - y1) / B.h : 1;
  const m = q => [r4(x1 + (q[0] - B.x) * sx), r4(y1 + (q[1] - B.y) * sy), ...q.slice(2)];
  if (s.t === 'pen') return { ...s, pts: s.pts.map(m) };
  if (s.t === 'text') return { ...s, a: m(s.a), s: r4(Math.max(0.008, (s.s || 0.04) * sy)) };
  return { ...s, a: m(s.a), b: m(s.b) };
}

// What is left of a freehand stroke after erasing a circle (centre p normalized, radius r pixels)
function eraseStroke(s, p, r, R) {
  const P = q => [R.x + q[0] * R.w, R.y + q[1] * R.h], o = P(p);
  const inside = q => { const v = P(q); return Math.hypot(v[0] - o[0], v[1] - o[1]) < r; };
  const pts = [];
  s.pts.forEach((q, i) => {
    const a = s.pts[i - 1];
    if (a && distSeg(o, P(a), P(q)) < r) {  // densify near the eraser so the cut is clean
      const A = P(a), B = P(q), n = Math.ceil(Math.hypot(B[0] - A[0], B[1] - A[1]) / Math.max(1, r / 3));
      for (let j = 1; j < n; j++) pts.push(a.map((v, d) => r4(v + (q[d] - v) * j / n)));
    }
    pts.push(q);
  });
  if (!pts.some(inside)) return [s];
  const pieces = [];
  let run = [];
  for (const q of pts) {
    if (inside(q)) { if (run.length > 1) pieces.push(run); run = []; } else run.push(q);
  }
  if (run.length > 1) pieces.push(run);
  return pieces.map(pts => ({ ...s, pts }));
}
