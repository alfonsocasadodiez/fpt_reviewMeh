"""
What comes in from ShotGrid.

Action Menu Items (AMIs) are the buttons people click in ShotGrid. Their request says who clicked but can't prove it, so here
the shared `ami_key` only allows opening or creating a review page: who you are is settled by signing in with ShotGrid.
Webhooks are signed by ShotGrid (HMAC-SHA1 of the body with the secret token, in X-SG-SIGNATURE).
"""
import hashlib
import hmac
import html
import json
from urllib.parse import parse_qs

from .config import APP_NAME, ACCENT


async def read_payload(request):
    """AMIs post a form; JSON is accepted too, for scripts and tests."""
    body = await request.body()
    if 'json' in request.headers.get('content-type', ''):
        try:
            data = json.loads(body or b'{}')
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {k: v[-1] for k, v in parse_qs(body.decode('utf-8', 'replace'), keep_blank_values=True).items()}


def selection(payload):
    """(entity type, ids) picked in ShotGrid."""
    ids = []
    for x in str(payload.get('selected_ids') or payload.get('ids') or '').split(','):
        x = x.strip()
        if x.isdigit():
            ids.append(int(x))
    return payload.get('entity_type', ''), ids


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def valid_signature(secret, body, header):
    """ShotGrid sends 'sha1=<hexdigest>'."""
    if not secret or not header:
        return False
    return hmac.compare_digest('sha1=' + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest(), header.strip())


def events(payload):
    """The events in a webhook body: one, or several when the webhook delivers in batches."""
    data = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    batch = data.get('deliveries')
    items = [d.get('data', d) for d in batch if isinstance(d, dict)] if isinstance(batch, list) else [data]
    return [e['meta'] for e in items if isinstance(e, dict) and isinstance(e.get('meta'), dict)]


def page(title, body='', url=None):
    """The small page an AMI answers with; it forwards to `url` when given."""
    refresh = f'<meta http-equiv="refresh" content="0; url={html.escape(url, quote=True)}">' if url else ''
    spinner = '<div class="spinner"></div>' if url else ''
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{html.escape(title)} — {html.escape(APP_NAME)}</title>{refresh}
<style>
  body {{ font-family: system-ui, sans-serif; background: #0d1117; color: #e6edf3; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 40px; text-align: center; max-width: 520px; }}
  .spinner {{ border: 3px solid #30363d; border-top-color: {ACCENT}; border-radius: 50%; width: 36px; height: 36px; animation: spin .8s linear infinite; margin: 0 auto 20px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  h2 {{ color: {ACCENT}; margin: 0 0 10px; }} p {{ color: #8b949e; font-size: .9rem; }} a {{ color: {ACCENT}; }} li {{ text-align: left; margin: 4px 0; }}
</style></head>
<body><div class="card">{spinner}<h2>{html.escape(title)}</h2>{body}</div></body></html>"""
