#  檢查表辨識服務（Pipelines）

透過視覺語言模型讀取表頭、量測項目與手寫數值，再將辨識結果寫入 **PostgreSQL**，供 Dashboard 顯示與分析。

## 架構（對應流程圖）

```text
Open WebUI／API測試頁面
   └─ POST /process
        ▼
FastAPI（aniki_api.py）
   ├─ 驗證 API Key
   ├─ 接收單一或多個 PDF／圖片
   ├─ 檢查同名檔案
   └─ 呼叫 Aniki.py
          ├─ PyMuPDF：PDF轉圖片
          ├─ metadatareader.py：讀取表頭資料
          ├─ rowreader.py：讀取固定格式資料列
          ├─ generic_rowreader.py：讀取一般表格
          ├─ generic_fix.py：修正辨識結果
          ├─ Ollama qwen2.5vl:7b：AI影像辨識
          └─ neon_db.py
                 ▼
          本地 PostgreSQL（REALDB）
                 ▼
          Dashboard顯示與SPC分析
```

* 使用者只需要呼叫 FastAPI，不需要分別執行每個 Python 檔案。
* AI 辨識由 Ollama 的 `qwen2.5vl:7b` 模型執行。
* `neon_db.py` 名稱沿用舊版，但目前實際連接的是本地 PostgreSQL。
* FastAPI 啟動點為 `aniki_api:app`，容器連接埠為 `8000`。
* Open WebUI 透過 `openwebui_aniki_pipe.py` 將附件傳送到 FastAPI。

## 啟動步驟

正式整合時由專案根目錄的 `docker-compose.yml` 統一啟動，不需要手動執行 Python。

### 1. 在專案根目錄建立 `.env`

```env
DB_PASSWORD=請設定本地PostgreSQL密碼
ANIKI_API_KEY=請設定一組較長的英文數字
```

### 2. 啟動Docker服務

先開啟 Docker Desktop，再於專案根目錄執行：

```bash
docker compose up -d --build
```

查看容器狀態：

```bash
docker compose ps
```

### 3. 第一次使用時還原資料庫

只有全新 PostgreSQL 第一次需要執行。

Windows PowerShell：

```powershell
cmd /c "docker compose exec -T postgres pg_restore -U postgres -d REALDB --clean --if-exists --no-owner < REALDB_backup.dump"
```

如果資料庫中已經有正式資料，不要重複執行，以免覆蓋原有資料。

### 4. 第一次使用時下載Ollama模型

```bash
docker compose exec ollama ollama pull qwen2.5vl:7b
```

確認模型：

```bash
docker compose exec ollama ollama list
```

### 5. 測試Pipelines服務

健康檢查：

```text
http://localhost:8000/health
```

正常結果應包含：

```json
{
  "status": "ok",
  "aniki_exists": true,
  "api_key_configured": true
}
```

API 測試頁面：

```text
http://localhost:8000/docs
```

在 `POST /process` 中填入：

* `x-api-key`：與 `.env` 的 `ANIKI_API_KEY` 相同
* `file`：選擇 PDF 或圖片
* `pages`：留空表示全部頁面，也可填 `1` 或 `1,2,3`
* `replace_existing`：第一次使用選擇 `false`
* `request_id`：例如 `integration-test-001`

成功結果應包含：

```json
{
  "success": true,
  "neon_success": true,
  "file_name": "檢表5.pdf",
  "row_count": 17
}
```

其中 `neon_success` 為舊版欄位名稱，目前代表資料是否成功寫入本地 PostgreSQL。

## Open WebUI設定

第一次在新的 Open WebUI 環境使用時，需要手動匯入 Pipe。

1. 開啟 `http://localhost:3000`
2. 進入 `Workspace → Functions`
3. 建立新的 Function
4. 貼上 `openwebui_aniki_pipe.py`
5. 儲存並啟用
6. 在 Valves 設定：

```text
API_URL = http://pipelines:8000
API_KEY = 與根目錄.env相同
TIMEOUT_SECONDS = 7200
DEFAULT_PAGES = 留空或填1
```

完成後回到聊天頁面：

1. 選擇 Aniki 模型
2. 上傳 PDF 或圖片
3. 輸入「開始辨識」
4. 等待辨識結果與資料庫寫入結果

Open WebUI 的 Function、Valves、API Key 與聊天紀錄儲存在 `openwebui_data` Docker volume，不會自動跟著 GitHub 移動，因此新電腦第一次仍需匯入。

## 功能

* 支援 PDF、PNG、JPG、JPEG、WEBP。
* 支援單一或多個檔案上傳。
* 可指定 PDF 處理頁碼。
* 讀取品號、製程、機台、流水號、日期、時間與操作者等表頭資料。
* 讀取名義值、實際值、上下公差與手寫量測內容。
* 支援固定格式及一般表格格式。
* 自動整理與修正 AI 辨識結果。
* 檢查資料庫中是否已有相同檔名。
* 可選擇是否覆蓋同名資料。
* 相同檔案短時間重複送出時，可沿用最近一次成功結果。
* 將辨識結果寫入本地 PostgreSQL。
* 提供人工確認頁面及確認結果保存功能。
* 提供 `/health` 健康檢查。
* 提供 `/file-status` 同名檔案查詢。
* 提供 `/process` 檔案辨識入口。
* 提供 Swagger API 測試介面。

## 檔案結構

```text
pipelines/
├─ Aniki.py
│  └─ 辨識主程式，負責整合PDF處理、AI辨識與資料整理
│
├─ aniki_api.py
│  └─ FastAPI入口，接收檔案並呼叫Aniki.py
│
├─ app.py
│  └─ 人工確認網頁與確認資料API
│
├─ metadatareader.py
│  └─ 讀取品號、製程、機台、日期等表頭資料
│
├─ rowreader.py
│  └─ 讀取固定格式的量測資料列
│
├─ generic_rowreader.py
│  └─ 讀取格式較不固定的一般表格
│
├─ generic_fix.py
│  └─ 修正及整理一般表格辨識結果
│
├─ neon_db.py
│  └─ 將辨識結果寫入PostgreSQL
│
├─ openwebui_aniki_pipe.py
│  └─ Open WebUI與FastAPI之間的連接程式
│
├─ check_pdf_direction.py
│  └─ 人工檢查PDF頁面方向的除錯工具
│
├─ Dockerfile
│  └─ 建立並啟動Pipelines容器
│
├─ requirements.txt
│  └─ Python套件清單
│
├─ .env.example
│  └─ 環境變數範例
│
├─ .dockerignore
│  └─ Docker建置時不放入容器的檔案
│
├─ .gitignore
│  └─ 不提交到GitHub的本機檔案
│
└─ README.md
   └─ 使用與整合說明
```
