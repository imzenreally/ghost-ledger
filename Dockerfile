FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 GHOST_LEDGER_STATE=/data/ledger.sqlite3
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 ledger \
    && useradd --uid 10001 --gid ledger --no-create-home ledger \
    && mkdir /data && chown ledger:ledger /data && chmod 0700 /data
COPY dashboard.py ledger_store.py ./
COPY dashboard_templates/ ./dashboard_templates/
USER 10001:10001
EXPOSE 8897
# Mount /data writable (uid 10001); root filesystem may be read-only.
# Set GHOST_LEDGER_PASSWORD_HASH and GHOST_LEDGER_SECRET_KEY privately.
# Publish ONLY on 10.0.0.10:8897 (or loopback), LAN/VPN only; no public proxy.
# No access log: credentials, URLs and financial rows are not logged.
# One worker makes the bounded in-memory login limiter coherent across threads.
CMD ["gunicorn", "--bind", "0.0.0.0:8897", "--workers", "1", "--threads", "4", "--worker-tmp-dir", "/dev/shm", "--umask", "0077", "dashboard:app"]
