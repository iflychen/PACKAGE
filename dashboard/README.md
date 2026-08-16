# SPC 品管管制圖（前端）

iPQC + SPC 專題的前端。選擇**品號製程**與**球標尺寸**，畫出 **I-MR 管制圖**，並標示哪些是**異常值**（超規/失控）、哪些是**標準值**。

## 架構（對應流程圖）

```
瀏覽器 (React 頁面)
   └─ fetch ─▶ Next.js API route (/api/*)        ← 本專案
                  ├─ 從 DB 替身取規格/管制線/量測值  (lib/db.ts)
                  └─ POST ─▶ Python SPC service /spc/build-chart-data
                                 └─ 回 points + limits（含異常旗標）
```

- **前端只打自己的 `/api/*`**，不直接碰 Python（所以沒有 CORS 問題）。
- **SPC 判定全部由 Python 算**（USL/LSL、UCL/LCL、Western/Nelson 規則）；前端只負責畫圖與標色。
- `lib/db.ts` 是 **PostgreSQL 替身**，內含種子資料。之後接真實 DB 時，只要改寫 `lib/db.ts` 裡的 `listProcessSummaries()` 與 `getFeature()`，前端與 API route 都不用動。

## 啟動步驟

需要**同時跑兩個服務**：Python SPC service 與本前端。

### 1) 先啟動 Python SPC service（在 `RealProject-main` 資料夾）

```bash
cd ../RealProject-main
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
uvicorn app.main:app --reload  # 預設 http://127.0.0.1:8000
```

### 2) 再啟動前端（在 `spc-dashboard` 資料夾）

```bash
npm install
npm run dev                    # http://localhost:3000
```

瀏覽器打開 http://localhost:3000 即可。

> 若 Python 不在預設位址，複製 `.env.local.example` 成 `.env.local` 改 `SPC_API_BASE`。

## 功能

- 製程 / 尺寸下拉選單（資料來自 `/api/processes`）。
- I-MR 管制圖：USL/LSL（紅虛線）、UCL/LCL（橘虛線）、CL（綠線）。
- 點的顏色：🟢 標準值、🟠 管制失控、🔴 超出規格；異常點放大。
- 滑鼠移到點上顯示量測值、時間與**違反的規則**（中文）。
- 右側面板：判定統計、界線數值、異常點清單。
- 範例含一個「未啟用管制線（Phase I）」的尺寸（深度_D1），示範只用 USL/LSL 判定的情況。

## 檔案結構

```
spc-dashboard/
├─ app/
│  ├─ page.tsx              主頁面（選單 + 串接 + 摘要面板）
│  ├─ layout.tsx / globals.css
│  └─ api/
│     ├─ processes/route.ts 製程/尺寸清單
│     └─ chart/route.ts     呼叫 Python build-chart-data
├─ components/
│  └─ ControlChart.tsx      Recharts 管制圖
└─ lib/
   ├─ db.ts                 DB 替身（種子資料；之後換真實 PostgreSQL）
   ├─ spcClient.ts          server-side 呼叫 Python
   ├─ types.ts              與 Python 對齊的型別
   └─ labels.ts             規則代碼 → 中文、點狀態/顏色
```

## 之後可擴充

- Cp/Cpk/Ppk 面板：呼叫 `POST /spc/capability`。
- Xbar-R / Xbar-S 圖：用 `POST /spc/calculate-trial-limits`（送 subgroups）。
- MR（移動全距）副圖。
- 時間區間篩選、異常處置紀錄、AI 摘要。
