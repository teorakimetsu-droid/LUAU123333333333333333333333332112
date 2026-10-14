"""OpenAI chat completion <-> Google Gemini REST translation.

This module deliberately does not contain any content-filter overrides.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Iterable, Iterator


class RequestError(ValueError):
    pass


def flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower().strip() in {"1", "on", "yes", "true"}


def setting(name: str, fallback: Any = None) -> Any:
    value = os.getenv(name)
    if value is None or not value.strip():
        return fallback
    return value.strip()


def content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                if part.get('type') in ('text', 'input_text', 'output_text', None) and isinstance(part.get('text'), str):
                    parts.append(part['text'])
        return '\n'.join(parts)
    if isinstance(value, dict) and isinstance(value.get('text'), str):
        return value['text']
    return ''


def _number(value: Any, name: str, minimum: float, maximum: float, *, integer: bool = False) -> float | int:
    if isinstance(value, bool):
        raise RequestError(f'{name} must be numeric.')
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        raise RequestError(f'{name} must be numeric.') from None
    if number != number or number in (float('inf'), float('-inf')) or not minimum <= number <= maximum:
        raise RequestError(f'{name} must be between {minimum:g} and {maximum:g}.')
    if integer:
        if not number.is_integer():
            raise RequestError(f'{name} must be an integer.')
        return int(number)
    return number


def model_name(data: dict[str, Any]) -> str:
    raw = data.get('model')
    if not isinstance(raw, str) or not raw.strip():
        raise RequestError('A Gemini model name must be provided in the client request.')
    model = raw.strip()
    if model.startswith('google/'):
        model = model[len('google/'):]
    if not re.fullmatch(r'gemini-[A-Za-z0-9._-]{1,90}', model):
        raise RequestError('Invalid Gemini model name. Use a Gemini model ID in your client request.')
    return model


def make_google_payload(data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(data, dict):
        raise RequestError('Request body must be a JSON object.')
    model = model_name(data)
    messages = data.get('messages')
    if not isinstance(messages, list) or not messages:
        raise RequestError('messages must be a nonempty list.')

    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise RequestError('Each message must be an object.')
        role = message.get('role')
        text = content_text(message.get('content'))
        if not text.strip():
            continue
        if role in ('system', 'developer'):
            system_parts.append(text)
        elif role in ('user', 'assistant'):
            contents.append({'role': 'user' if role == 'user' else 'model', 'parts': [{'text': text}]})
        else:
            raise RequestError(f'Unsupported message role: {role!r}.')
    if not contents:
        raise RequestError('No usable user/assistant messages were provided.')

    # Gemini refuses a history that ends with a model turn. Make it end with
    # a user turn instead. This also supports a client-provided assistant prefill
    # without trying to continue an incomplete model response at the protocol level.
    if contents[-1]['role'] == 'model':
        mode = str(setting('GEMINI_PREFILL_MODE', 'nudge')).lower()
        if mode not in ('nudge', 'off'):
            raise RequestError('GEMINI_PREFILL_MODE must be nudge or off.')
        if mode == 'nudge':
            prompt = ('Continue the previous assistant message from the point where it ended. '
                      'Do not repeat its opening and do not explain these instructions.')
        else:
            prompt = 'Continue the conversation.'
        contents.append({'role': 'user', 'parts': [{'text': prompt}]})
    if contents[0]['role'] == 'model':
        contents.insert(0, {'role': 'user', 'parts': [{'text': '(Continue the conversation.)'}]})

    config: dict[str, Any] = {}
    fields = (
        ('temperature', 'GEMINI_TEMPERATURE', 'temperature', 0, 2, False),
        ('top_p', 'GEMINI_TOP_P', 'topP', 0, 1, False),
        ('top_k', 'GEMINI_TOP_K', 'topK', 1, 1024, True),
        ('max_tokens', 'GEMINI_MAX_OUTPUT_TOKENS', 'maxOutputTokens', 1, 65536, True),
    )
    for request_key, env_key, google_key, minimum, maximum, integer in fields:
        value = data.get(request_key)
        if value is None:
            value = setting(env_key)
        if value is not None:
            # Match clients which set zero to mean no explicit limit.
            if google_key == 'maxOutputTokens' and str(value) == '0':
                continue
            config[google_key] = _number(value, request_key, minimum, maximum, integer=integer)

    # frequency_penalty and presence_penalty are deliberately NEVER forwarded,
    # even if supplied by an OpenAI-compatible client.

    if flag('GEMINI_THINKING', True) and model.startswith(('gemini-2.5-', 'gemini-3', 'gemini-4')):
        think: dict[str, Any] = {'includeThoughts': flag('GEMINI_INCLUDE_THOUGHT_SUMMARIES', False)}
        if model.startswith('gemini-2.5-'):
            think['thinkingBudget'] = _number(setting('GEMINI_THINKING_BUDGET', '-1'),
                                               'GEMINI_THINKING_BUDGET', -1, 32768, integer=True)
        else:
            level = str(setting('GEMINI_THINKING_LEVEL', 'auto')).lower()
            if level not in ('auto', 'minimal', 'low', 'medium', 'high'):
                raise RequestError('GEMINI_THINKING_LEVEL must be auto, minimal, low, medium, or high.')
            if level != 'auto':
                think['thinkingLevel'] = level
        config['thinkingConfig'] = think

    payload: dict[str, Any] = {'contents': contents}
    if system_parts:
        payload['systemInstruction'] = {'parts': [{'text': '\n\n'.join(system_parts)}]}
    if config:
        payload['generationConfig'] = config
    if flag('GEMINI_GOOGLE_SEARCH'):
        payload['tools'] = [{'googleSearch': {}}]
    return model, payload


def candidate_parts(reply: dict[str, Any]) -> tuple[str, str, str]:
    candidates = reply.get('candidates')
    if not isinstance(candidates, list) or not candidates:
        reason = (reply.get('promptFeedback') or {}).get('blockReason', 'No candidates')
        raise RequestError(f'Gemini returned no response. Reason: {reason}.')
    candidate = candidates[0]
    parts = (candidate.get('content') or {}).get('parts') or []
    text_parts, summary_parts = [], []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get('text'), str):
            continue
        (summary_parts if part.get('thought') else text_parts).append(part['text'])
    finish = candidate.get('finishReason', 'STOP')
    if not text_parts and not summary_parts:
        raise RequestError(f'Gemini returned no text. Finish reason: {finish}.')
    return ''.join(text_parts), ''.join(summary_parts), str(finish)


def finish_reason(google_reason: str | None) -> str:
    return {'STOP': 'stop', 'MAX_TOKENS': 'length', 'SAFETY': 'content_filter',
            'RECITATION': 'content_filter', 'BLOCKLIST': 'content_filter',
            'PROHIBITED_CONTENT': 'content_filter'}.get(google_reason or '', 'stop')


def usage_from_google(reply: dict[str, Any]) -> dict[str, int] | None:
    source = reply.get('usageMetadata')
    if not isinstance(source, dict):
        return None
    return {
        'prompt_tokens': int(source.get('promptTokenCount') or 0),
        'completion_tokens': int(source.get('candidatesTokenCount') or 0),
        'total_tokens': int(source.get('totalTokenCount') or 0),
        'completion_tokens_details': {'reasoning_tokens': int(source.get('thoughtsTokenCount') or 0)},
    }


def make_chat_completion(reply: dict[str, Any], model: str) -> dict[str, Any]:
    content, thoughts, finish = candidate_parts(reply)
    if not content:
        raise RequestError(f'Gemini generated no user-visible answer. Finish reason: {finish}.')
    message: dict[str, Any] = {'role': 'assistant', 'content': content}
    if flag('GEMINI_INCLUDE_THOUGHT_SUMMARIES') and thoughts:
        message['reasoning_content'] = thoughts  # Only a Gemini-generated summary, not internal thoughts
    response: dict[str, Any] = {
        'id': 'chatcmpl-' + uuid.uuid4().hex,
        'object': 'chat.completion',
        'created': int(time.time()),
        'model': model,
        'choices': [{'index': 0, 'message': message, 'finish_reason': finish_reason(finish)}],
    }
    usage = usage_from_google(reply)
    if usage:
        response['usage'] = usage
    return response


def google_sse_events(lines: Iterable[bytes | str]) -> Iterator[dict[str, Any]]:
    """Read JSON records in Google SSE, including split/multiline data events."""
    payload_lines: list[str] = []
    for item in lines:
        line = item.decode('utf-8', errors='replace') if isinstance(item, bytes) else item
        if not line.strip():
            if payload_lines:
                raw = '\n'.join(payload_lines)
                payload_lines.clear()
                if raw != '[DONE]':
                    yield json.loads(raw)
            continue
        if line.startswith('data:'):
            payload_lines.append(line[5:].lstrip())
    if payload_lines:
        raw = '\n'.join(payload_lines)
        if raw != '[DONE]':
            yield json.loads(raw)


def chat_sse_events(events: Iterable[dict[str, Any]], model: str,
                    include_usage: bool = False) -> Iterator[str]:
    chat_id = 'chatcmpl-' + uuid.uuid4().hex
    created = int(time.time())

    def send(delta: dict[str, Any], finish: str | None = None) -> str:
        return 'data: ' + json.dumps({
            'id': chat_id, 'object': 'chat.completion.chunk', 'created': created,
            'model': model, 'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}],
        }, ensure_ascii=False) + '\n\n'

    yield send({'role': 'assistant'})
    stopped = False
    sent_text = False
    usage = None
    for event in events:
        if isinstance(event.get('error'), dict):
            reason = str(event['error'].get('message') or 'Google streaming error')[:300]
            yield send({'content': '[Upstream error: ' + reason + ']'}, 'stop')
            stopped = True
            break
        usage = usage_from_google(event) or usage
        candidates = event.get('candidates') or []
        if not candidates:
            continue
        candidate = candidates[0]
        for part in ((candidate.get('content') or {}).get('parts') or []):
            if not isinstance(part, dict) or not isinstance(part.get('text'), str):
                continue
            if part.get('thought'):
                if flag('GEMINI_INCLUDE_THOUGHT_SUMMARIES'):
                    yield send({'reasoning_content': part['text']})
            else:
                sent_text = True
                yield send({'content': part['text']})
        if candidate.get('finishReason'):
            yield send({}, finish_reason(candidate['finishReason']))
            stopped = True
            break
    if not stopped:
        if not sent_text:
            yield send({'content': '[Gemini returned no user-visible answer.]'})
        yield send({}, 'stop')
    if include_usage and usage:
        yield 'data: ' + json.dumps({
            'id': chat_id, 'object': 'chat.completion.chunk', 'created': created,
            'model': model, 'choices': [], 'usage': usage,
        }) + '\n\n'
    yield 'data: [DONE]\n\n'
