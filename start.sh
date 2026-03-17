#!/bin/bash
# 載入環境變數
set -a
source "$(dirname "$0")/.env"
set +a

exec gunicorn -k eventlet -w 1 --bind 0.0.0.0:5200 --reload app:app
