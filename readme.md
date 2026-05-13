啟動指令

```bash
./start.sh
```

`start.sh` 會載入 `.env`，並使用 Gunicorn `gthread` worker：

```bash
gunicorn --worker-class gthread --workers 1 --threads 12 --bind 0.0.0.0:5200 app:app
```

注意：

- 不再使用 `eventlet`，避免 monkey patch 影響 APNs HTTP/2 發送。
- 即時協作 presence 目前仍放在記憶體，因此 `WEB_WORKERS` 預設維持 `1`。
- 開發時若需要自動 reload，可設定 `GUNICORN_RELOAD=true ./start.sh`。
