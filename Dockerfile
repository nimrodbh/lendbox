FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY lendbox lendbox
RUN pip install --no-cache-dir .
COPY seed seed
ENV LENDBOX_SEED_DIR=/app/seed \
    PORT=8000 \
    FORWARDED_ALLOW_IPS=127.0.0.1
EXPOSE 8000
# Migrate, then serve. PORT is set by the host (Render uses 10000). --proxy-headers trusts X-Forwarded-For only
# from FORWARDED_ALLOW_IPS, so per-IP rate limits see the real client and a client cannot spoof its address.
CMD ["sh", "-c", "python -m lendbox.db.migrate && exec uvicorn lendbox.web.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips=${FORWARDED_ALLOW_IPS}"]
