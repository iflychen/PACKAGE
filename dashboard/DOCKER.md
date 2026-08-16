# SPC Dashboard — Docker 使用說明

Next.js 14 前端。負責畫管制圖、製程能力、Phase I 核准流程。

本身不含資料庫也不含 SPC 計算服務，兩者都靠環境變數指過去。

## 這個服務需要什麼

| 依賴 | 環境變數 | 說明 |
| --- | --- | --- |
| PostgreSQL | `DATABASE_URL` | 讀規格、量測值、管制界線 |
| Python SPC service | `SPC_API_BASE` | 所有統計判定都在那邊算 |

兩者缺一頁面都出不來，所以 compose 裡要跟 `postgres`、`spc-api` 同一個網路。

## 單獨建置與執行

```bash
docker build -t spc-dashboard .

cp .env.example .env      # 填 DATABASE_URL / SPC_API_BASE
docker run -p 3000:3000 --env-file .env spc-dashboard
```

開 http://localhost:3000

## 放進 docker-compose.yml

```yaml
services:
  dashboard:
    build: ./dashboard
    image: spc-dashboard:latest
    ports:
      - "3000:3000"
    environment:
      DATABASE_URL: postgresql://spc:${POSTGRES_PASSWORD}@postgres:5432/spc
      SPC_API_BASE: http://spc-api:8000
      SETTINGS_PASSWORD: ${SETTINGS_PASSWORD}
    depends_on:
      postgres:
        condition: service_healthy
      spc-api:
        condition: service_started
```

`depends_on` 的 `service_healthy` 很重要。Next.js 幾秒就起來，PostgreSQL 要十幾秒才 ready，不等的話第一次開頁面會看到連線錯誤。`postgres` 服務要配一個 healthcheck：

```yaml
  postgres:
    # 版本要 >= 匯出來源的版本。目前資料來自 Neon 的 PostgreSQL 18.4，
    # 用 16 的話 dump 檔會還原失敗。
    image: postgres:18
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U spc"]
      interval: 5s
      retries: 10
```

## 資料庫

`lib/neon.ts` 用 node-postgres（`pg`），可以連任何 PostgreSQL 12+。
連線字串含 `sslmode=require` 或指向 `neon.tech` 時會自動啟用 TLS，
所以同一份程式接容器內的 postgres 和 Neon 雲端都可以。

需要的資料表與 view：

- 表：`品號`、`製程`、`機台`、`球標尺寸`、`工件`、`測量值`、`事件紀錄`、`管制圖`
- View：`事件使用區間`、`工件_含事件`

兩個 view 用 `LEAD()` 推算事件區間的邊界，是管制圖分區間的基礎，
建資料庫時一定要一併建立。schema 細節見 `HANDOFF.md`。

## 已知限制

`lib/config.ts` 的三個設定（`min_samples`、`cpk_threshold`、
`auto_create_control_limit`）存在 Node process 的記憶體裡，
**容器重啟就會回到預設值**。需要持久化的話要改存資料庫。

## image 大小

約 150MB。多階段建置 + `output: "standalone"`，
編譯器和完整 `node_modules` 都留在 builder 階段沒有進最終 image。
