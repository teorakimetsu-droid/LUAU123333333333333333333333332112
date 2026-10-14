"""Railway-ready Gemini proxy with human-friendly homepage and safe diagnostics."""
import json
import logging
import os
import random
import threading
import time
from queue import Empty, Full, Queue
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

from core import (RequestError, chat_sse_events, google_sse_events,
                  make_chat_completion, make_google_payload)
from diagnostics import classify_google_error, safe_retry_after

HOMEPAGE_HTML = '<!doctype html>\n<html lang="en">\n<head>\n  <meta charset="utf-8">\n  <meta name="viewport" content="width=device-width,initial-scale=1">\n  <meta name="color-scheme" content="dark">\n  <meta name="description" content="Gemini proxy connection page and API diagnostics.">\n  <title>Gemini Gateway • Connection</title>\n  <style>\n    :root { color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;\n      --bg:#090d17; --card:#131b29; --card2:#182436; --border:#2a3749; --text:#edf4fe; --muted:#9db1c8; --green:#6fe5ad; --blue:#82b8ff; }\n    * { box-sizing: border-box; }\n    body { min-height:100vh; margin:0; color:var(--text); background:radial-gradient(ellipse at 85% 0%, #1e3442 0, transparent 39%),radial-gradient(ellipse at 7% 96%,#17243a 0, transparent 38%),var(--bg); }\n    .wrap { max-width:880px; padding:58px 22px 64px; margin:0 auto; }\n    .mark { font-size:12px; font-weight:800; letter-spacing:.17em; color:var(--green); text-transform:uppercase; }\n    h1 { font-size:clamp(32px,5.8vw,51px); font-weight:770; letter-spacing:-.045em; line-height:1.08; margin:14px 0 13px; }\n    .lede { color:var(--muted); max-width:630px; line-height:1.65; margin:0 0 34px; font-size:15px; }\n    .panel { background:linear-gradient(135deg,rgba(24,36,54,.98),rgba(16,24,39,.98)); border:1px solid var(--border); border-radius:16px; padding:26px; margin-bottom:18px; box-shadow:0 14px 36px #0002; }\n    .status { display:flex; align-items:center; gap:11px; padding:14px 15px; margin-bottom:24px; background:#0d1723; border:1px solid #294032; border-radius:10px; font-size:14px; }\n    .dot { width:10px; height:10px; flex-shrink:0; background:#eabf76; border-radius:50%; box-shadow:0 0 0 4px #eabf7622; }\n    .dot.up { background:var(--green); box-shadow:0 0 0 4px #6fe5ad25; }\n    .dot.down { background:#ff858e; box-shadow:0 0 0 4px #ff858e20; }\n    h2 { margin:0 0 8px; font-size:18px; letter-spacing:-.018em; }\n    .small { font-size:13px; color:var(--muted); line-height:1.6; margin:0; }\n    .endpoint { margin:20px 0 0; }\n    .label { display:block; margin-bottom:10px; font-size:12px; letter-spacing:.055em; font-weight:750; text-transform:uppercase; color:#c5d4e8; }\n    .copyline { display:flex; align-items:center; gap:8px; background:#0b1421; border:1px solid #344860; border-radius:10px; padding:8px; }\n    code { flex:1; color:#afe7f1; padding:8px 10px; overflow-wrap:anywhere; font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:13px; }\n    button { border-radius:8px; background:#d7f8e6; color:#132725; padding:10px 16px; border:0; font:inherit; font-weight:750; font-size:13px; cursor:pointer; min-width:80px; }\n    button:hover { background:#adedd1; }\n    button:focus-visible,a:focus-visible { outline:3px solid var(--blue); outline-offset:3px; }\n    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:18px; }\n    ul { margin:12px 0 0; padding-left:20px; color:#c5d3e4; font-size:13px; line-height:1.85; }\n    strong { color:var(--text); }\n    a { color:#91c9ff; }\n    .note { margin-top:16px; background:#152032; border-left:3px solid var(--blue); padding:12px 14px; border-radius:4px 9px 9px 4px; font-size:13px; line-height:1.65; color:#c4d8eb; }\n    footer { padding-top:14px; font-size:12px; color:#8a9fb7; }\n    @media(max-width:640px) { .wrap { padding-top:37px; } .panel { padding:20px; } .grid { grid-template-columns:1fr; } .copyline { align-items:stretch; flex-direction:column; } button { align-self:flex-end; } }\n  </style>\n</head>\n<body>\n  <main class="wrap">\n    <div class="mark">Gemini Gateway / Railway</div>\n    <h1>Your gateway is here.</h1>\n    <p class="lede">This page shows where your proxy lives and where your API client should send requests. The homepage is not the chat-completions endpoint.</p>\n    <section class="panel" aria-labelledby="connection-title">\n      <div class="status" role="status" aria-live="polite"><span class="dot" id="dot"></span><span id="status">Checking local proxy status…</span></div>\n      <h2 id="connection-title">Connection details</h2>\n      <p class="small">Copy the appropriate address into an OpenAI-compatible client. The client sends a POST request with its Gemini model ID and Google AI API key.</p>\n      <div class="endpoint">\n        <span class="label">Full API endpoint</span>\n        <div class="copyline"><code id="full-endpoint">Loading…</code><button type="button" data-copy="full-endpoint">Copy</button></div>\n      </div>\n      <div class="endpoint">\n        <span class="label">Base URL (for clients that append /chat/completions)</span>\n        <div class="copyline"><code id="base-endpoint">Loading…</code><button type="button" data-copy="base-endpoint">Copy</button></div>\n      </div>\n      <div class="endpoint">\n        <span class="label">Health check (local server only)</span>\n        <div class="copyline"><code id="health-endpoint">Loading…</code><button type="button" data-copy="health-endpoint">Copy</button></div>\n      </div>\n    </section>\n    <div class="grid">\n      <section class="panel" aria-labelledby="steps-title">\n        <h2 id="steps-title">Connect your client</h2>\n        <ul>\n          <li>Use the <strong>full API endpoint</strong>, or the <strong>base URL</strong> if your app needs one.</li>\n          <li>Send your own Google AI API key securely as a Bearer token.</li>\n          <li>Supply a Gemini model ID in the API request; this site does not choose one.</li>\n        </ul>\n      </section>\n      <section class="panel" aria-labelledby="errors-title">\n        <h2 id="errors-title">Common errors</h2>\n        <ul>\n          <li><strong>429:</strong> Google API quota or rate limit. A different URL won\'t increase limits.</li>\n          <li><strong>502 / 504:</strong> Google connection or timeout. Inspect Railway logs.</li>\n          <li><strong>400 / 404:</strong> Unsupported parameters or unavailable model ID.</li>\n          <li><strong>401 / 403:</strong> Check API key and Google project access.</li>\n        </ul>\n      </section>\n    </div>\n    <div class="note"><strong>What the green status means:</strong> This Railway web service is responding. It does not verify Google API access, quota, or model availability. API requests can still fail even while the webpage is online.</div>\n    <footer>Native thinking, optional Google Search, and streaming are handled server-side. The website does not accept or store API keys.</footer>\n  </main>\n  <script>\n  (() => {\n    const base = window.location.origin;\n    document.getElementById(\'full-endpoint\').textContent = base + \'/v1/chat/completions\';\n    document.getElementById(\'base-endpoint\').textContent = base + \'/v1\';\n    document.getElementById(\'health-endpoint\').textContent = base + \'/health\';\n    for (const button of document.querySelectorAll(\'[data-copy]\')) {\n      button.addEventListener(\'click\', async () => {\n        const text = document.getElementById(button.dataset.copy).textContent;\n        try {\n          await navigator.clipboard.writeText(text);\n          const old = button.textContent;\n          button.textContent = \'Copied\';\n          window.setTimeout(() => { button.textContent = old; }, 1500);\n        } catch {\n          button.textContent = \'Select URL\';\n          const range = document.createRange();\n          range.selectNodeContents(document.getElementById(button.dataset.copy));\n          const selection = window.getSelection();\n          selection.removeAllRanges();\n          selection.addRange(range);\n        }\n      });\n    }\n    fetch(\'/health\', {cache:\'no-store\'})\n      .then(r => { if (!r.ok) throw new Error(\'offline\'); return r.json(); })\n      .then(d => { if (d.status !== \'ok\') throw new Error(\'offline\');\n        document.getElementById(\'dot\').classList.add(\'up\');\n        document.getElementById(\'status\').textContent = \'Proxy online · Google API access not checked\';\n      })\n      .catch(() => {\n        document.getElementById(\'dot\').classList.add(\'down\');\n        document.getElementById(\'status\').textContent = \'Health check failed · inspect Railway deployment logs\';\n      });\n  })();\n  </script>\n</body>\n</html>\n'


app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
logger = logging.getLogger('gemini-railway')
GOOGLE_API = 'https://generativelanguage.googleapis.com/v1beta/models/'


@app.after_request
def api_cors(response):
    # Browser-based API clients need an OPTIONS preflight for Authorization.
    # Credentials are supplied as a bearer header, never cookies.
    if request.path in ('/', '/v1/chat/completions', '/chat/completions'):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Authorization, Content-Type, X-API-Key'
        response.headers['Access-Control-Expose-Headers'] = 'Retry-After'
    return response


def fail(message, status, *, kind='proxy_error', retry_after=None):
    body = {'error': {'message': str(message), 'type': kind, 'code': status}}
    if retry_after is not None:
        body['error']['retry_after_seconds'] = retry_after
    response = jsonify(body)
    response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    if retry_after is not None:
        response.headers['Retry-After'] = str(retry_after)
    return response


@app.get('/')
def homepage():
    response = Response(HOMEPAGE_HTML, mimetype='text/html')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.get('/health')
def health():
    # Liveness only: this does NOT test Gemini credentials or model quota.
    result = jsonify({'status': 'ok', 'service': 'gemini-railway', 'upstream_checked': False,
                      'endpoints': {'base': '/v1', 'chat_completions': '/v1/chat/completions'}})
    result.headers['Cache-Control'] = 'no-store'
    return result


@app.get('/v1/chat/completions')
@app.get('/chat/completions')
def api_instructions():
    return jsonify({'status': 'ready', 'method': 'POST',
                    'message': 'This endpoint accepts OpenAI-compatible JSON POST requests; GET is for diagnostics only.',
                    'health': '/health'})


def request_key():
    auth = request.headers.get('Authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return request.headers.get('x-api-key', '').strip()


def _upstream_post(url, payload, headers, streaming):
    """Bounded retries for temporary upstream failures, not quota bypass.

    A long/unknown 429 is returned immediately; requests at a daily quota
    limit cannot be fixed by retries. Never log or put keys in the URL.
    """
    try:
        retries = max(0, min(int(os.getenv('GEMINI_MAX_RETRIES', '1')), 2))
    except ValueError:
        retries = 1

    for attempt in range(retries + 1):
        try:
            response = requests.post(url, json=payload, headers=headers,
                                     timeout=(15, 290), stream=streaming)
        except (requests.Timeout, requests.ConnectionError) as exc:
            if attempt == retries:
                raise
            logger.warning('Temporary Google connection failure (%s), retrying once', type(exc).__name__)
        else:
            delay = safe_retry_after(response.headers.get('Retry-After'))
            transient = response.status_code in (502, 503, 504)
            short_limit = response.status_code == 429 and delay is not None and delay <= 2
            if attempt == retries or not (transient or short_limit):
                return response
            response.close()
            if short_limit:
                time.sleep(delay)
                continue
            logger.warning('Google returned transient HTTP %s; retrying', response.status_code)
        time.sleep(min(1.0 + attempt * 1.5 + random.uniform(0, 0.25), 3.0))
    raise RuntimeError('unreachable')


def _stream_with_heartbeat(upstream, model, include_usage):
    """Emit SSE keepalive comments when Gemini's thinking takes a while.

    Reading requests.iter_lines() is blocking, so it happens in a worker.
    This keeps reverse proxies from mistaking a quiet stream for a dead one.
    """
    items = Queue(maxsize=128)
    sentinel = object()
    closed = threading.Event()

    def produce():
        try:
            google_events = google_sse_events(upstream.iter_lines(decode_unicode=False))
            for piece in chat_sse_events(google_events, model, include_usage=include_usage):
                if closed.is_set():
                    break
                while not closed.is_set():
                    try:
                        items.put(piece, timeout=1)
                        break
                    except Full:
                        continue
        except (requests.RequestException, ValueError, TypeError):
            logger.warning('Google streaming connection interrupted')
            if not closed.is_set():
                try:
                    items.put('data: ' + json.dumps({'error': {'message': 'Google stream interrupted.',
                                                                'type': 'upstream_connection_error'}}) + '\n\n', timeout=2)
                    items.put('data: [DONE]\n\n', timeout=2)
                except Full:
                    pass
        finally:
            upstream.close()
            try:
                items.put(sentinel, timeout=2)
            except Full:
                pass

    worker = threading.Thread(target=produce, name='gemini-stream', daemon=True)
    worker.start()
    try:
        while True:
            try:
                piece = items.get(timeout=15)
            except Empty:
                if not worker.is_alive():
                    break
                yield ': keepalive\n\n'
                continue
            if piece is sentinel:
                break
            yield piece
    finally:
        closed.set()
        upstream.close()


@app.post('/')
@app.post('/chat/completions')
@app.post('/v1/chat/completions')
def completions():
    key = request_key()
    if not key:
        return fail('Missing Google API key. Send Authorization: Bearer <YOUR_KEY>.', 401,
                    kind='authentication_error')
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail('A JSON request body is required.', 400, kind='invalid_request_error')
    try:
        model, payload = make_google_payload(data)
    except RequestError as exc:
        return fail(str(exc), 400, kind='invalid_request_error')

    streaming = data.get('stream') is True
    suffix = 'streamGenerateContent?alt=sse' if streaming else 'generateContent'
    url = GOOGLE_API + quote(model, safe='-._') + ':' + suffix
    headers = {'Content-Type': 'application/json', 'x-goog-api-key': key}
    try:
        upstream = _upstream_post(url, payload, headers, streaming)
    except requests.Timeout:
        return fail('Timed out waiting for Google Gemini. Try streaming or a shorter response.', 504,
                    kind='upstream_timeout')
    except requests.RequestException:
        logger.warning('Could not connect to Google Gemini (credentials not logged)')
        return fail('Proxy is online, but it could not connect to Google Gemini.', 502,
                    kind='upstream_connection_error')

    if upstream.status_code >= 400:
        try:
            source = upstream.json()
        except (ValueError, TypeError):
            source = {}
        kind, message, retry_after = classify_google_error(upstream.status_code, source,
                                                              upstream.headers.get('Retry-After'), key)
        upstream.close()
        logger.warning('Gemini API returned HTTP %s (%s)', upstream.status_code, kind)
        code = upstream.status_code if 400 <= upstream.status_code < 600 else 502
        return fail(message, code, kind=kind, retry_after=retry_after)

    if streaming:
        opts = data.get('stream_options')
        include_usage = bool(opts.get('include_usage')) if isinstance(opts, dict) else False
        response = Response(stream_with_context(_stream_with_heartbeat(upstream, model, include_usage)),
                            mimetype='text/event-stream',
                            headers={'Cache-Control': 'no-cache, no-transform',
                                     'X-Accel-Buffering': 'no', 'X-Content-Type-Options': 'nosniff'})
        return response

    try:
        output = make_chat_completion(upstream.json(), model)
    except (ValueError, TypeError, RequestError):
        return fail('Google Gemini returned an empty or invalid reply. Check model compatibility and thinking settings.',
                    502, kind='upstream_response_error')
    finally:
        upstream.close()
    return jsonify(output)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
