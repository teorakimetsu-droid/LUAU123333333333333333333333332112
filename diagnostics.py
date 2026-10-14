"""Human-readable, secret-safe upstream error messages."""
import re


def safe_retry_after(raw):
    """Return small positive integer delay, or None for missing/unusable value."""
    if raw is None:
        return None
    try:
        delay = int(float(str(raw).strip()))
    except (TypeError, ValueError, OverflowError):
        return None
    return delay if 0 <= delay <= 86400 else None


def classify_google_error(status, body, retry_header=None, key=''):
    error = body.get('error') if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    reason = str(error.get('message') or '').strip()
    # API key may be embedded in upstream error text; never return it.
    if key:
        reason = reason.replace(key, '[REDACTED]')
    reason = re.sub(r'(?i)(key=)[^&\s]+', r'\1[REDACTED]', reason)
    reason = reason[:450]
    retry = safe_retry_after(retry_header)
    if retry is None:
        # Google may return retryDelay in error details instead of a header.
        details = error.get('details', [])
        if isinstance(details, list):
            for item in details:
                if isinstance(item, dict):
                    text = item.get('retryDelay')
                    if isinstance(text, str) and re.fullmatch(r'\d+(?:\.\d+)?s', text):
                        retry = safe_retry_after(text[:-1])
                        if retry is not None:
                            break
    if status == 429:
        head = 'Google Gemini rate limit or quota reached for this API key and model.'
        advice = f' Retry after about {retry} seconds.' if retry is not None else ' Check usage and rate limits in Google AI Studio.'
        return 'rate_limit_error', head + advice + (f' Google: {reason}' if reason else ''), retry
    if status == 503:
        return 'upstream_unavailable', 'Google Gemini is temporarily unavailable. Retry shortly.' + (f' Google: {reason}' if reason else ''), retry
    if status in (401, 403):
        return 'authentication_error', 'Google Gemini rejected the API key or access permissions.' + (f' Google: {reason}' if reason else ''), retry
    if status == 404:
        return 'model_not_found', 'The requested Gemini model was not found or is unavailable to this API key.' + (f' Google: {reason}' if reason else ''), retry
    if status == 400:
        return 'invalid_request_error', 'Google Gemini rejected the model or request parameters.' + (f' Google: {reason}' if reason else ''), retry
    return 'upstream_error', f'Google Gemini returned HTTP {status}.' + (f' Google: {reason}' if reason else ''), retry
