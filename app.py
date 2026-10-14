"""Railway-hostable, OpenAI-compatible Gemini bridge."""
import logging
import os
from urllib.parse import quote

import requests
from flask import Flask, Response, jsonify, request, stream_with_context

from core import (RequestError, chat_sse_events, google_sse_events,
                  make_chat_completion, make_google_payload)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 4 * 1024 * 1024
logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
logger = logging.getLogger('gemini-railway')


def fail(message, status):
    return jsonify({'error': {'message': str(message), 'type': 'proxy_error', 'code': status}}), status


@app.get('/')
@app.get('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'gemini-railway'})


@app.post('/')
@app.post('/chat/completions')
@app.post('/v1/chat/completions')
def completions():
    key = request.headers.get('Authorization', '')
    if key.lower().startswith('bearer '):
        key = key[7:].strip()
    else:
        key = request.headers.get('x-api-key', '').strip()
    if not key:
        return fail('Supply your Google AI API key using Authorization: Bearer <key>.', 401)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return fail('A JSON object is required.', 400)
    try:
        model, payload = make_google_payload(data)
    except RequestError as exc:
        return fail(str(exc), 400)

    streaming = data.get('stream') is True
    suffix = 'streamGenerateContent?alt=sse' if streaming else 'generateContent'
    url = ('https://generativelanguage.googleapis.com/v1beta/models/'
           + quote(model, safe='-._') + ':' + suffix)
    # Key stays in a header, never a URL/query string or log message.
    headers = {'Content-Type': 'application/json', 'x-goog-api-key': key}
    try:
        upstream = requests.post(url, json=payload, headers=headers,
                                 timeout=(15, 290), stream=streaming)
    except requests.Timeout:
        return fail('Google AI request timed out.', 504)
    except requests.RequestException:
        logger.exception('Google AI connection failed (no credentials logged)')
        return fail('Could not reach Google AI.', 502)

    if upstream.status_code >= 400:
        try:
            source = upstream.json()
            message = (source.get('error') or {}).get('message', '')
        except (ValueError, AttributeError, TypeError):
            message = 'Google AI rejected the request.'
        upstream.close()
        # Never pass an API key embedded in a Google error body back to clients.
        message = str(message).replace(key, '[REDACTED]')[:600] or 'Google AI rejected the request.'
        logger.warning('Upstream response failed: %s', upstream.status_code)
        status = upstream.status_code if upstream.status_code in (400, 401, 403, 404, 429, 503) else 502
        return fail(message, status)

    if streaming:
        def generate():
            try:
                parsed = google_sse_events(upstream.iter_lines(decode_unicode=False))
                include_usage = bool((data.get('stream_options') or {}).get('include_usage'))
                yield from chat_sse_events(parsed, model, include_usage=include_usage)
            except (requests.RequestException, ValueError, TypeError) as exc:
                logger.warning('Interrupted stream: %s', type(exc).__name__)
                # A JSON error status cannot be emitted after an SSE response has begun.
                import json
                yield 'data: ' + json.dumps({'error': {'message': 'Upstream stream interrupted.'}}) + '\n\n'
                yield 'data: [DONE]\n\n'
            finally:
                upstream.close()
        return Response(stream_with_context(generate()), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    try:
        upstream_json = upstream.json()
        output = make_chat_completion(upstream_json, model)
    except (ValueError, TypeError, RequestError):
        return fail('Google AI returned an empty or invalid response.', 502)
    finally:
        upstream.close()
    return jsonify(output)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')))
