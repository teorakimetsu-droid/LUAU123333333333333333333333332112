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
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

from core import (RequestError, chat_sse_events, google_sse_events,
                  make_chat_completion, make_google_payload)
from diagnostics import classify_google_error, safe_retry_after

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
    response = send_file('static/index.html', mimetype='text/html')
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
