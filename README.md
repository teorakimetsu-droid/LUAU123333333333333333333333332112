# Gemini Railway Features

A small, self-contained Python 3.13 Gemini bridge for Railway. It speaks the OpenAI Chat Completions request format and uses Google's REST API. It is an **alternative** to the cloned GeminiForJanitors application; it does not bundle the upstream code, Redis-backed user accounts, multi-key rotation, stored commands, or moderation overrides.

## What is included

- Native Gemini thinking via `generationConfig.thinkingConfig`. Gemini 3/4 models use optional `thinkingLevel`; Gemini 2.5 models use `thinkingBudget`.
- Optional thought **summaries** via the nonstandard OpenAI `reasoning_content` field; raw private reasoning is not available.
- Optional Google Search tool for supported Gemini models.
- `temperature`, `top_p`, `top_k`, and `max_tokens` support, configurable in the client request or Railway Variables.
- `systemInstruction`, regular chat history, and typed text message support.
- A continuation nudge for trailing assistant-prefill messages.
- Real incremental Server-Sent Events (SSE) when `stream=true`; non-streaming also works.
- Sanitized Google API errors, API key in the HTTP header (never query parameters), timeout handling, and `/health` endpoint.
- **No** model selector, tunnel/provider selector, frequency penalty, presence penalty, or content-filter override.

## Railway deployment

1. Create a repository containing **Dockerfile**, **app.py**, **core.py**, **requirements.txt**, and optionally **.dockerignore** in the repository root.
2. Connect the repository to Railway. The file must be named `Dockerfile`, not `Dockerfile.txt`.
3. Make sure Railway is using the Dockerfile builder and clear any custom Start Command. Deploy.
4. In Railway Networking, create a public domain. Configure the optional Variables below as desired.
5. Set your OpenAI-compatible client's base URL to `https://YOUR-RAILWAY-DOMAIN/v1` (or full completion endpoint to `https://YOUR-RAILWAY-DOMAIN/v1/chat/completions`, depending on the client). The client should send a valid **Gemini model ID** and a Google API key via `Authorization: Bearer ...`. You can also use `x-api-key`.

### Railway Variables

| Variable | Default | Effect |
|---|---|---|
| `GEMINI_THINKING` | `1` | Requests native reasoning support where available. `0` omits configuration; some models still reason by default. |
| `GEMINI_THINKING_LEVEL` | `auto` | Gemini 3 and 4: `auto`, `low`, `medium`, or `high` (model-dependent; `minimal` also exists on some models). `auto` uses the model default. |
| `GEMINI_THINKING_BUDGET` | `-1` | Gemini 2.5: `-1` for dynamic reasoning, or a supported integer token budget. Model restrictions apply. |
| `GEMINI_INCLUDE_THOUGHT_SUMMARIES` | `0` | `1` exposes any Gemini-produced *summaries* in `reasoning_content`, separate from the answer. A client's UI may ignore this field. |
| `GEMINI_GOOGLE_SEARCH` | `0` | Set `1` to enable Google Search grounding for compatible models. May have usage costs. |
| `GEMINI_PREFILL_MODE` | `nudge` | `nudge` adds a continuation request after a trailing assistant message; `off` adds a neutral continuation request. |
| `GEMINI_TEMPERATURE` | unset | Optional default sampling temperature (0 to 2). |
| `GEMINI_TOP_P` | unset | Optional default top-p (0 to 1). |
| `GEMINI_TOP_K` | unset | Optional default top-k (1 to 1024). |
| `GEMINI_MAX_OUTPUT_TOKENS` | unset | Optional default output token maximum (1 to 65536). Note that reasoning tokens can count toward this limit. |

Client request values take precedence over the generation defaults. The bridge **ignores** `frequency_penalty` and `presence_penalty`, even if a client includes them. The model is always taken from the client request. No model choice is hardcoded into the project.

### Example request

```bash
curl -X POST "https://YOUR-RAILWAY-DOMAIN/v1/chat/completions" \
  -H "Authorization: Bearer YOUR_GOOGLE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"Explain photosynthesis briefly."}],"stream":false}'
```

The exact Gemini models available to your account are controlled by Google's API. If a model or thinking level is unsupported, Google may respond with HTTP 400/404. The bridge returns that error rather than secretly choosing another model.

### If Railway reports `useradd: user 'proxy' already exists`

The Dockerfile now checks whether the `proxy` account exists before creating it. Replace your old Dockerfile with the one in this package, commit the change and redeploy. This also works with base images that do not already have a `proxy` account.

## Testing locally

Requires Python 3.13 or newer to match Docker (the `core.py` tests also work with Python 3.10+):

```bash
python -m pip install -r requirements.txt pytest
python -m pytest tests -q
python app.py
```

Health check: `GET http://localhost:5000/health`.

## Known limitations

This is **not a patch** for the upstream repository: it replaces the cloned upstream app with a focused OpenAI-compatible bridge, and therefore does not preserve upstream commands, multi-key rotation, or persistent user storage. Only text messages are supported; file/image/audio content parts are not forwarded. An SSE connection can still be cut off by an upstream network issue, in which case the server sends an SSE error event and terminates. A complete Docker build and live Google API call could not be performed in the authoring environment; run a real deployment smoke test.

Protect API keys: never commit them to the repository, and do not share public URLs that embed secrets in the query string. Anyone with their own valid Google API key can make requests to the public endpoint; if you need additional access controls, configure them before publishing the service broadly.
