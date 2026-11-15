Aniki 完整整合包：全表單手寫辨識 + unclear + SQLite 四分類人工修正

本包把辨識規則、CMM/一般檢查單人工修正、SQLite 四分類整合在同一份更新中。

功能
1. 一般檢查單：使用 generic_rowreader.py 逐格辨識藍色手寫欄位；不確定時不猜。
2. CMM 檢查單：保留原本 CMM 流程，rowreader.py 將不確定結果區分為 unknown、unclear、null。
3. 辨識狀態：unknown = 有內容但無法判定寫了什麼；unclear = 字跡/影像看不清；null = 確認沒有填寫。
4. 人工修正：CMM 與一般檢查單辨識後都建立 review_path，進同一個人工修正頁面。
5. SQLite：review_cell_evaluations.verdict 可存 correct、incorrect、unclear、blank 四種人工評價；review_tasks 保存原始/修正資料。
6. SQLite 更新：服務啟動時建立新表及欄位；若舊評價表的 CHECK 規則不接受 unclear/blank，會重建評價表並保留舊紀錄。資料庫檔案不會由本包覆蓋。
7. 正確率依目前程式規則計算：correct / (correct + incorrect + missing rows + extra rows)；unclear 與正確空白另外統計，不列入分母。

檔案
  app.py                  Review UI、API、SQLite schema 與資料庫升級
  Aniki.py                CMM/generic 路由與 OCR 流程
  aniki_api.py            上傳處理及 CMM/generic review task 建立
  cmm_review_adapter.py   多頁 CMM/generic 輸出轉人工修正格式
  generic_rowreader.py    一般檢查單逐格手寫辨識與三態規則
  rowreader.py            CMM 讀列規則（unknown/unclear/null）
  套用更新.ps1           備份並複製六個程式檔
  test_review_flow.py     隔離式 SQLite / adapter 測試
  test_api_review.py      API 人工修正流程測試

套用前
- 本包是來源程式更新包，不會自動改動你的電腦、Docker 或 GitHub。
- 先停止 pipelines 服務，備份現有程式與 review_tasks.db。不要刪除或覆蓋 review_tasks.db。
- 請確認目標是專案中的 pipelines 程式目錄；套用腳本只覆蓋上列六個程式檔。
- 不會修改 metadatareader.py、組員其他模組、資料庫連線設定，也不會把確認結果自動寫入組員 PostgreSQL。

Windows PowerShell 套用方式
1. 解壓本包。
2. 在專案根目錄停止服務：
   docker compose stop pipelines
3. 備份 review_tasks.db（先用 docker compose config 檢查它實際掛載位置）。
4. 在本包資料夾執行：
   .\套用更新.ps1 -TargetDir "C:\Users\USER\Downloads\PACKAGE-main\PACKAGE-main\pipelines"
   請把路徑改成你電腦實際的 PACKAGE-main\pipelines。
5. 回到專案根目錄重建並啟動：
   docker compose build pipelines
   docker compose up -d pipelines
   docker compose logs -f pipelines
6. 服務啟動時 app.py 會初始化/升級 SQLite schema。確認 /health 成功後，分別上傳一份 CMM 與一份一般檢查單，確認兩者都有 review_path。
7. 在人工修正頁逐格標記正確、錯誤、不清楚、空白；確認後用 /api/reviews/{token}/accuracy 查看分類及統計。

重要資料行為
- OCR 的 unknown/unclear/null 是辨識值；SQLite 的 correct/incorrect/unclear/blank 是人工評價。兩者用途不同。
- 確認空白以 null 保留；辨識不確定值會保留在 Review 資料中，不會偷偷轉成數字。
- PDF 原檔不會嵌在修正頁；需對照漏列時，請同時開啟原始 PDF。
- 本包未在你的實際 Docker/Ollama/Open WebUI 環境部署；套用後仍需依上方方式重建並確認服務回傳 review_path。
