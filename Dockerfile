# Disease Correlation & Risk Engine
# Runs anywhere that takes a container: Render, Railway, Fly.io, Cloud Run, ECS,
# Azure Container Apps, or your own VM.
#
#   docker build -t dre .
#   docker run --rm -p 8000:8000 dre
#   open http://localhost:8000

FROM python:3.11-slim

# Fail fast, no .pyc clutter, unbuffered logs so the platform sees them live.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so application edits do not invalidate the wheel layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application. .dockerignore keeps tests, caches and the alternative UI out.
COPY . .

# Fail the build rather than the deploy if the configuration is inconsistent:
# every cross-reference, citation and Disease Master link is checked at import.
RUN python -c "from engine.config import get_config; v = get_config().validation; \
print('config:', v['counts']); \
assert v['ok'], v['errors']"

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

ENV PORT=8000
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request,sys; \
u='http://127.0.0.1:'+os.environ.get('PORT','8000')+'/api/health'; \
sys.exit(0 if urllib.request.urlopen(u, timeout=4).status == 200 else 1)"

# Shell form so $PORT is expanded — platforms inject the port they want.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips='*'
