# Save exactly as "Dockerfile", with NO .txt extension.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000 \
    GEMINI_THINKING=1 \
    GEMINI_THINKING_LEVEL=auto \
    GEMINI_THINKING_BUDGET=-1 \
    GEMINI_GOOGLE_SEARCH=0 \
    GEMINI_INCLUDE_THOUGHT_SUMMARIES=0 \
    GEMINI_PREFILL_MODE=nudge

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py core.py ./

RUN useradd --system --no-create-home --shell /usr/sbin/nologin proxy
USER proxy
EXPOSE 5000

# Railway's PORT variable is expanded at runtime.
CMD sh -c 'exec gunicorn --bind "0.0.0.0:${PORT:-5000}" --workers 1 --threads 8 --timeout 330 app:app'
