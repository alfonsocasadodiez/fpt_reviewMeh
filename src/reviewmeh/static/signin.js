'use strict';
/* Sign in with ShotGrid (its App Session Launcher): the user approves this app in a ShotGrid
 * tab, the server polls ShotGrid and signs this browser in as that user. Resolves to the user. */
async function signInWithShotGrid() {
  const win = window.open('', '_blank');  // opened inside the click, so popup blockers allow it
  const close = () => { try { win?.close(); } catch (e) { /* already gone */ } };
  const json = async r => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.statusText); return j; };
  try {
    const { id, url } = await json(await fetch('/api/login', { method: 'POST' }));
    if (win) win.location = url; else window.open(url, '_blank');
    for (const t0 = Date.now(); Date.now() - t0 < 300000;) {
      await new Promise(res => setTimeout(res, 2000));
      const { user } = await json(await fetch(`/api/login/${encodeURIComponent(id)}`));
      if (user) { close(); window.focus(); return user; }
    }
    throw new Error('Sign-in timed out');
  } catch (e) { close(); throw e; }
}
