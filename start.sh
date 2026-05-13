#!/bin/bash
# 載入環境變數
set -a
source "$(dirname "$0")/.env"
set +a

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-5200}"
WEB_WORKERS="${WEB_WORKERS:-1}"
WEB_THREADS="${WEB_THREADS:-12}"
GUNICORN_TIMEOUT="${GUNICORN_TIMEOUT:-120}"
GUNICORN_RELOAD="${GUNICORN_RELOAD:-false}"

reload_args=()
if [ "$GUNICORN_RELOAD" = "1" ] || [ "$GUNICORN_RELOAD" = "true" ]; then
  reload_args=(--reload)
fi

# 使用 gthread 避免 eventlet monkey patch 影響 APNs HTTP/2 client。
# presence 目前仍放在記憶體，因此預設維持 1 worker；需要擴充時再導入 shared pub/sub。
exec gunicorn \
  --worker-class gthread \
  --workers "$WEB_WORKERS" \
  --threads "$WEB_THREADS" \
  --bind "$HOST:$PORT" \
  --timeout "$GUNICORN_TIMEOUT" \
  "${reload_args[@]}" \
  app:app
