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
# 選填；未設定時使用下列預設值
VLM_MODEL=qwen2.5vl:7b
SPC_OLLAMA_MODEL=qwen2.5:7b
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

### 3. 資料庫自動初始化

`database-init` 會在首次啟動時檢查 REALDB：

* 資料庫完全空白時，自動還原根目錄的 `REALDB_backup.dump`。
* 已有必要資料表時直接略過，不會覆蓋既有資料。
* 若已有部分資料表但結構不完整，初始化會停止並提示錯誤，不會自動清除資料。

可用下列指令查看初始化紀錄：

```bash
docker compose logs database-init
```

### 4. 確認自動初始化完成

Docker 啟動時會自動：

* 下載 Aniki 所需的 `qwen2.5vl:7b` 視覺模型與 SPC 所需模型；已存在的模型不會重複下載。
* 首次啟動時還原 REALDB 的必要資料表。
* 將 `openwebui_aniki_pipe.py` 安裝或更新到 Open WebUI。
* 啟用 Aniki，並把 `aniki` 設成新對話的預設模型。

第一次啟動需等待模型下載完畢。可用下列指令確認模型與初始化紀錄：

```bash
docker compose exec ollama ollama list
docker compose logs aniki-openwebui-init
```

### 5. 測試 Pipelines 服務

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

## Open WebUI 使用方式

1. 開啟 `http://localhost:3000` 並登入。
2. 開啟新對話；Docker 已將 Aniki 設為預設模型，不需要手動選擇。
3. 上傳 PDF 或圖片並按送出；訊息內容可留空或任意填寫，不需要輸入特定辨識指令。
4. 等待辨識結果與資料庫寫入結果。
5. 若資料庫已有同名檔案，Aniki 會先詢問是否以新資料取代；確認後才會重新辨識及更新，取消則保留舊資料。若瀏覽器未顯示確認視窗，可直接回覆「取代」或「取消」。

`aniki-openwebui-init` 是可重複執行的初始化服務。每次 Docker 啟動都會確認 Aniki 已安裝、啟用並更新到專案內的版本；既有的聊天紀錄及手動調整過的 Valves 仍保存在 `openwebui_data` volume。

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
├─ openwebui_bootstrap.py
│  └─ Docker啟動時自動安裝、啟用Aniki並設為預設模型
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
