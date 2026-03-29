FROM python:3.11-slim

# ── System packages ──────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    supervisor \
    curl \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-srp \
    tesseract-ocr-srp-latn \
    && rm -rf /var/lib/apt/lists/*

# ── Python dependencies ───────────────────────────────────────────────────────
WORKDIR /app
COPY agent/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && pip install --no-cache-dir "litellm[proxy]" \
    && playwright install chromium --with-deps

# ── App source ────────────────────────────────────────────────────────────────
COPY agent/   /app/agent/
COPY web/     /app/web/
COPY config/  /app/config/
COPY memory/  /app/memory/

# ── Service configs ───────────────────────────────────────────────────────────
# Kopiramo konfige u /app/ umesto u /etc/ jer tamo imamo pune dozvole
COPY nginx.hf.conf      /app/nginx.conf
COPY supervisord.conf   /app/supervisord.conf

# ── Runtime directories & Permissions ─────────────────────────────────────────
# Sve stavljamo u /tmp ili unutar /app jer su to jedina mesta gde HF dozvoljava pisanje
RUN mkdir -p /tmp/chroma /tmp/supervisor /tmp/nginx /tmp/data \
    && chmod -R 777 /tmp /app

# Kreiramo korisnika 1000
RUN useradd -m -u 1000 hfuser
RUN chown -R hfuser:hfuser /app /tmp

# HF Spaces traži port 7860
EXPOSE 7860

USER 1000

# KLJUČNA IZMENA: Pokrećemo supervisord sa konfigom iz /app/ koji smo upravo kopirali
CMD ["/usr/bin/supervisord", "-n", "-c", "/app/supervisord.conf"]
