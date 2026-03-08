# ─── ML Service Dockerfile ───────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# System deps (no TF C++ dependencies needed with pip)
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install ML dependencies
COPY ml/requirements.txt /app/ml/requirements.txt
RUN pip install --no-cache-dir -r ml/requirements.txt

# Copy ML source
COPY ml/ /app/ml/

# Directory for saved models
RUN mkdir -p /app/ml/saved_models

# Create non-root user
RUN adduser --disabled-password --gecos '' mluser && chown -R mluser /app
USER mluser

EXPOSE 8001

HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8001/health || exit 1

CMD ["uvicorn", "ml.api.server:app", "--host", "0.0.0.0", "--port", "8001", "--workers", "2"]
