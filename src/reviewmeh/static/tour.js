'use strict';
/* Guided tour: walks the controls on screen. Steps whose control isn't available (rights, collapsed panel) are skipped. */
const TOUR_STEPS = [
  ['#itemList', 'Versions', 'Click a version to play it. Shift or Cmd-click several to play them back to back, then right-click to act on all of them.'],
  ['#modeSeg', 'Version, Mini-cut, whole cut', 'Watch one version on its own, with the shots either side, or everything in cut order.'],
  ['#statusBtn', 'Status', 'Changes the version status in ShotGrid straight away.'],
  ['#btnCompare', 'Compare', 'Puts another version of the shot next to this one: side by side, wipe, blend or difference. The wipe line turns from its round handle.'],
  ['#stage', 'Viewer', 'Wheel zooms and middle-drag pans. Draw straight on the frame with the tools below. Right-click a drawing to cut, copy or paste it to another frame.'],
  ['#tools', 'Drawing tools', 'Brush follows pen pressure, marker keeps one width. With Select, drag a drawing to move it or a handle to resize it. The eraser cuts strokes where it touches.'],
  ['#colors', 'Colours', 'Nine presets, three slots of your own and a free picker.'],
  ['#sizes', 'Size and opacity', 'Set them before drawing, or select a drawing and change it.'],
  ['#track', 'Timeline', 'Wheel zooms, Shift-wheel pans. Click a drawing’s marker, then Shift-click where it should end, to make it last. Shift-drag anywhere else sets In and Out.'],
  ['#ioBtns', 'In and Out', 'Set where playback starts and stops, and clear it. It works across shots too. Keys: I, O and Esc.'],
  ['#tc', 'Frames or timecode', 'Click to switch how frames are shown and typed everywhere.'],
  ['#rtabs', 'Notes, Shot notes, Details', 'Notes on this version, every note on the shot with its drawings, or the ShotGrid fields you choose.'],
  ['#composer', 'Posting', 'Post sends your comment, any files you attach and every unposted drawing on this version, one note per drawn frame. Drawings stay saved until you post them.'],
  ['#btnExport', 'Download and export', 'This frame, the movie, or an export: PDF report, annotated frames as ZIP, notes as CSV.'],
  ['#btnShare', 'Share', 'Copy the studio link, or create a link that expires, with or without guests and a password.'],
  ['#btnPresent', 'Present', 'Everyone in the review follows your playback and sees your pointer.'],
  ['#btnPublish', 'Publish to SG', 'Sends your notes to ShotGrid, one thread per version with each frame and file next to its text, and a link back here. You see what will go first.'],
  ['#btnTour', 'That’s it', 'Hover any control for a short explanation, and press ? for every shortcut.'],
];

(function tour() {
  const visible = sel => {
    const el = document.querySelector(sel);
    if (!el || el.closest('[hidden]')) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
  };
  let steps = [], i = 0, back, spot, card;

  function place() {
    const [sel, title, text] = steps[i], el = document.querySelector(sel);
    if (!el) return close();
    const pad = 4, r = el.getBoundingClientRect();
    Object.assign(spot.style, { left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
    card.innerHTML = `<h3>${esc(title)}</h3><p>${esc(text)}</p>
      <div class="tf"><span class="tn">${i + 1} / ${steps.length}</span>
        <button data-go="close">Close</button>${i ? '<button data-go="-1">Back</button>' : ''}
        <button class="primary" data-go="1">${i === steps.length - 1 ? 'Done' : 'Next'}</button></div>`;
    // Beside the control if there's room, else above or below it
    const w = card.offsetWidth, h = card.offsetHeight, gap = 12, W = innerWidth, H = innerHeight;
    let x, y;
    if (r.bottom + gap + h < H) { x = r.left; y = r.bottom + gap; }
    else if (r.top - gap - h > 0) { x = r.left; y = r.top - gap - h; }
    else if (r.right + gap + w < W) { x = r.right + gap; y = r.top; }
    else if (r.left - gap - w > 0) { x = r.left - gap - w; y = r.top; }
    else { x = (W - w) / 2; y = (H - h) / 2; }
    Object.assign(card.style, { left: `${Math.max(8, Math.min(x, W - w - 8))}px`, top: `${Math.max(8, Math.min(y, H - h - 8))}px` });
  }
  function go(d) {
    const n = i + d;
    if (n >= steps.length) return close();
    if (n >= 0) { i = n; place(); }
  }
  function onKey(e) {
    e.stopPropagation(); e.preventDefault();
    if (e.key === 'Escape') close();
    if (e.key === 'ArrowRight' || e.key === 'Enter') go(1);
    if (e.key === 'ArrowLeft') go(-1);
  }
  function close() {
    for (const el of [back, spot, card]) el?.remove();
    back = spot = card = null;
    document.removeEventListener('keydown', onKey, true);
    removeEventListener('resize', place);
  }
  $('#btnTour').addEventListener('click', () => {
    steps = TOUR_STEPS.filter(([sel]) => visible(sel));
    i = 0;
    back = document.createElement('div'); back.id = 'tourBack';
    spot = document.createElement('div'); spot.id = 'tourSpot';
    card = document.createElement('div'); card.id = 'tourCard';
    card.addEventListener('click', e => {
      const v = e.target.closest('[data-go]')?.dataset.go;
      if (v === 'close') close(); else if (v) go(+v);
    });
    document.body.append(back, spot, card);
    document.addEventListener('keydown', onKey, true);
    addEventListener('resize', place);
    place();
  });
})();
