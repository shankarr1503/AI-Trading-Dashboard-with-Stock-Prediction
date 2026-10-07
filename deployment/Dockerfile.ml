# ─── ML Service ──────────────────────────────────────────────
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY ml/requirements.txt /app/ml/requirements.txt
RUN pip install -r ml/requirements.txt

COPY shared/ /app/shared/
COPY ml/ /app/ml/

RUN mkdir -p /app/ml/saved_models \
    && adduser --disabled-password --gecos '' mluser && chown -R mluser /app
USER mluser

EXPOSE 8001
HEALTHCHECK --interval=60s --timeout=15s --start-period=90s --retries=3 \
    CMD curl -fsS http://localhost:8001/health || exit 1

CMD ["uvicorn", "ml.api.server:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
