# ─── Hugging Face Spaces — single-container build ───────────────────────────
# All services (nginx, chromadb, litellm, trinity-agent) run via supervisord.
# HF Spaces requires the app to listen on port 7860.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System packages ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    curl \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-ita \
    tesseract-ocr-spa \
    tesseract-ocr-fra \
    tesseract-ocr-ell \
    tesseract-ocr-srp \
    tesseract-ocr-srp-latn \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app
COPY agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir "litellm[proxy]==1.40.14" \
    && playwright install chromium --with-deps

# ── App source ────────────────────────────────────────────────────────────────
COPY agent/   /app/agent/
COPY web/     /app/web/
COPY config/  /app/config/
COPY memory/  /app/memory/

# ── Service configs ───────────────────────────────────────────────────────────
COPY nginx.hf.conf    /etc/nginx/nginx.conf
COPY supervisord.conf /app/supervisord.conf
COPY proxy.py         /app/proxy.py

# ── Runtime directories ───────────────────────────────────────────────────────
RUN mkdir -p /data/chroma /tmp/nginx \
    && chmod -R 777 /data /tmp/nginx

# HF Spaces expects a non-root user with uid 1000
RUN useradd -m -u 1000 hfuser \
    && chown -R hfuser:hfuser /app /data /tmp/nginx

# HF Spaces requires port 7860
EXPOSE 7860

USER 1000

CMD ["/usr/bin/supervisord", "-n", "-c", "/app/supervisord.conf"]
