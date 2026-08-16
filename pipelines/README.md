# Pipelines（組員 B）

這個資料夾可直接放進共用 repo 根目錄，位置應為 `project-repo/pipelines/`。真正的 `.env`、測試 PDF、辨識輸出、除錯圖片與既有 SQLite 資料不包含在交付包內。

## 提供的服務

- FastAPI 啟動點：`aniki_api:app`
- 容器連接埠：`8000`
- 健康檢查：`GET /health`
- 辨識入口：`POST /process`
- 依賴服務：PostgreSQL、Ollama
- 預設 Ollama 模型：`qwen2.5vl:7b`
- `check_pdf_direction.py`、`generic_fix.py` 為原專案的人工維護工具，不是容器啟動點

## 組員 A 的 docker-compose 設定

把下列服務合併進 repo 根目錄唯一的 `docker-compose.yml`；若實際 service 名稱不是 `postgres` 或 `ollama`，同步修改 URL/主機名稱。

```yaml
services:
  pipelines:
    build: ./pipelines
    ports:
      - "8000:8000"
    environment:
      DATABASE_URL: postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}
      ANIKI_API_KEY: ${ANIKI_API_KEY}
      OLLAMA_URL: http://ollama:11434/api/chat
      VLM_MODEL: qwen2.5vl:7b
      VLM_RECHECK_MODEL: qwen2.5vl:7b
      OLLAMA_KEEP_ALIVE: 10m
      REVIEW_DB_PATH: /app/data/review_tasks.db
    volumes:
      - ./pipelines/data:/app/data
    depends_on:
      - postgres
      - ollama
    restart: unless-stopped
```

請在 repo 根目錄的 `.env` 設定 `POSTGRES_USER`、`POSTGRES_PASSWORD`、`POSTGRES_DB` 與 `ANIKI_API_KEY`。`DATABASE_URL` 已改成連到 Compose 內的 `postgres`，不是雲端 Neon；但 PostgreSQL 仍需先建立本專題原本使用的資料表結構。

## 單獨測試

```bash
docker build -t aniki-pipelines ./pipelines
docker run --rm -p 8000:8000 \
  -e DATABASE_URL='postgresql://postgres:postgres@host.docker.internal:5432/aniki' \
  -e ANIKI_API_KEY='change-me' \
  -e OLLAMA_URL='http://host.docker.internal:11434/api/chat' \
  aniki-pipelines
```

啟動後開啟 `http://localhost:8000/health`。正式整合時應由根目錄 `docker-compose.yml` 統一啟動。
