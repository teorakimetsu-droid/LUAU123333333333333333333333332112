FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN curl -L \
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
    -o /usr/local/bin/cloudflared && \
    chmod +x /usr/local/bin/cloudflared

RUN git clone --depth=1 \
    https://github.com/vu5eruz/GeminiForJanitors.git \
    /app/GeminiForJanitors

WORKDIR /app/GeminiForJanitors

ENV GFJPROXY_ADMIN="Railway"
ENV GFJPROXY_COOLDOWN="0"
ENV GFJPROXY_DEVELOPMENT="yes"
ENV GFJPROXY_PROCESS_TIMEOUT="300"
ENV GFJPROXY_XUID_SECRET="Railway"

CMD ["sh", "-c", "uv run --no-dev gunicorn -b 0.0.0.0:${PORT} -k gevent -w 1 -t ${GFJPROXY_PROCESS_TIMEOUT} 'gfjproxy.app:create_app()'"]
