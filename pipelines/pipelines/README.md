# Aniki Pipelines：三次元與手寫檢查單人工修正版

此資料夾是可直接放進 `PACKAGE-main/pipelines` 的完整 Pipelines 程式目錄。專案根目錄既有的 `docker-compose.yml` 保留在原位，它負責建置本資料夾並掛載 SQLite 持久化目錄。

## 辨識與人工修正

- CMM／三次元檢查單走 `rowreader.py`；一般手寫檢查單走 `generic_rowreader.py`。兩種結果都建立同一種 SQLite 人工修正任務，並提供 `/review/{token}` 頁面。
- OCR 原值與人工修正值分開保存。每格可保存 `unknown`、`unclear`、確定空白（SQLite `NULL`）、數值字串或勾勾 `✓`。數值以字串保存，保留正負號、小數位及前導零。
- 表單可直接輸入數值，也可選 `unknown`、`unclear`、`null（確定空白）`、`✓` 快捷按鈕。確認時服務端依 OCR 值和人工修正值推導正確、錯誤、不清楚或空白統計；使用者不必另外輸入評價標籤。
- 頁碼是定位資訊，不納入格數正確率。空白與未解決的 `unknown`／`unclear` 不進入正確率分母；CMM 多頁資料會合併成一張表並保留來源頁碼。
- 本版不在 OCR 階段寫入 PostgreSQL。人工修正後的原始值、修正值和統計保存於 SQLite；沒有自動覆寫組員的 SPC／PostgreSQL 資料。

## SQLite 路徑

`app.py` 讀取 `REVIEW_DB_PATH`。Compose 設定為 `/app/data/review_tasks.db`，並以 `pipelines_data` volume 持久保存。若沒有設定環境變數，開發模式預設使用 `pipelines/data/review_tasks.db`。初始化時建立或升級 SQLite schema，包含 `correct / incorrect / unclear / blank` 統計類別；原始 OCR 和人工修正的欄位值仍分別保存。

## 安裝到既有專案

1. 先備份現有 `pipelines` 資料夾和 SQLite 資料庫。不要刪除 Docker volume，也不要執行 `docker compose down -v`。
2. 將本更新包的 `pipelines` 資料夾合併到 `PACKAGE-main/pipelines`，同名檔案選擇覆蓋。不要把這些 Python 檔散放到專案根目錄。
3. 在 `docker-compose.yml` 所在的專案根目錄執行：

   ```powershell
   docker compose up -d --build --no-deps pipelines
   docker compose run --rm --no-deps aniki-openwebui-init
   docker compose restart openwebui
   docker compose ps pipelines openwebui
   ```

4. 等 Pipelines 和 Open WebUI 正常後，開啟 `http://localhost:3000`，選擇 Aniki，分別上傳一份三次元表單及一份一般手寫表單。兩者的回覆都應有人工修正連結。
5. 在修正頁檢查 `unknown`、`unclear`、數值、`✓` 和確定空白；送出後檢查 `/api/reviews/{token}/accuracy` 的統計。

若原本 Compose 沒有 `aniki-openwebui-init` 服務，先不要照第 3 步執行；需先把現有 Compose 的 Pipelines build 路徑、`REVIEW_DB_PATH` volume 和 Open WebUI 初始化服務對齊本專案設定。不要用舊的 README 替換或覆蓋根目錄 Compose。

## 主要檔案

| 檔案 | 用途 |
|---|---|
| `Aniki.py` | PDF 頁面處理、CMM／一般表單分流與 OCR 工作流；不直接寫正式資料庫 |
| `rowreader.py`、`generic_rowreader.py` | 三次元固定格式和一般手寫表單的逐格辨識及 `unknown / unclear / null` 保留 |
| `aniki_api.py`、`app.py` | 上傳 API、共用修正頁、SQLite schema 升級、人工值保存及準確率統計 |
| `cmm_review_adapter.py` | 將 CMM 和一般多頁辨識結果轉為共用修正表 |
| `openwebui_aniki_pipe.py`、`openwebui_bootstrap.py` | Open WebUI 上傳入口及啟動時安裝／更新 Aniki |
| `Dockerfile`、`requirements.txt` | 建置 Pipelines 容器 |
| `import_last_review.py` | 將先前保存的 `all_pages_result.json` 匯入共用修正頁，不重跑 OCR |

## 本機檢查

在此資料夾可執行：

```powershell
python -m pytest test_review_flow.py test_api_review.py test_openwebui_aniki_pipe.py test_openwebui_bootstrap.py -q
```

這些測試使用隔離的暫存 SQLite 和替身 OCR/API，不會連接或修改正式資料庫，也不會取代在你的 Docker、Ollama 和 Open WebUI 上做的實際上傳驗證。
