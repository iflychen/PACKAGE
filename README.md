# iPQC + SPC 智慧品管系統 — 完整部署手冊

本 repo 是專題「IPQC 檢驗表 AI 辨識 + SPC 統計製程管制」的**整套 Docker 部署程式碼**。
照著這份文件，可以在**一台全新的電腦**上，從安裝 Docker 開始，一路把整套系統跑起來。

> 主線以 **Windows 10 / 11 + WSL2 + Docker Desktop** 撰寫，
> macOS 與 Linux 的差異另外寫在 [附錄 A](#附錄-amacos--linux-的差異)。

> **三種安裝方式，看你的情況選：**
>
> | 情況 | 看哪份 | 現場要做什麼 |
> | --- | --- | --- |
> | 第一次、要從原始碼建置 | **本文件** | `docker compose up -d --build` |
> | 網路很慢 / 卡在 ghcr.io | **本文件 [8.1](#81選用網路太慢的話從-release-下載離線包)** | 從 Releases 下載離線包，跳過線上下載 |
> | 工廠機台、完全沒有外網 | [OFFLINE-INSTALL.md](OFFLINE-INSTALL.md) | USB 拷貝 + `docker load`，全程零網路 |

---

## 目錄

1. [這套系統在做什麼](#1-這套系統在做什麼)
2. [系統架構](#2-系統架構)
3. [服務與連接埠一覽](#3-服務與連接埠一覽)
4. [部署前的需求](#4-部署前的需求)
5. [步驟一：安裝 WSL2 與 Docker Desktop](#5-步驟一安裝-wsl2-與-docker-desktop)
6. [步驟二：安裝 Git 並取得原始碼](#6-步驟二安裝-git-並取得原始碼)
7. [步驟三：建立 `.env` 環境變數檔](#7-步驟三建立-env-環境變數檔)
8. [步驟四：第一次啟動](#8-步驟四第一次啟動)（含[從 Release 下載離線包](#81選用網路太慢的話從-release-下載離線包)）
9. [步驟五：驗證部署是否成功](#9-步驟五驗證部署是否成功)
10. [步驟六：第一次使用](#10-步驟六第一次使用)
11. [環境變數完整說明](#11-環境變數完整說明)
12. [資料庫說明](#12-資料庫說明)
13. [日常維運指令](#13-日常維運指令)
14. [疑難排解](#14-疑難排解)
15. [專案目錄結構](#15-專案目錄結構)
16. [安全性注意事項](#16-安全性注意事項)
17. [附錄 A：macOS / Linux 的差異](#附錄-amacos--linux-的差異)
18. [附錄 B：API 端點索引](#附錄-bapi-端點索引)

---

## 1. 這套系統在做什麼

整套系統由兩條主線組成：

**① 檢驗表辨識（Aniki）**
把 IPQC 的紙本檢查表（PDF／照片）丟進 Open WebUI 的對話框，系統用視覺語言模型
（`qwen2.5vl:7b`）讀出表頭（品號、製程、機台、流水號、日期、操作者）與每一列的
量測資料（名義值、上下公差、實際量測值），整理後寫進 PostgreSQL。

**② SPC 管制圖分析（Dashboard）**
從資料庫讀出量測值，交給 Python SPC 服務計算 I-MR／Xbar-R／Xbar-S 管制界線與
製程能力指標（Cp／Cpk／Cpm／Cpmk／Ppk），在網頁上畫出管制圖、標出超規與失控點，
並支援 **Phase I 試算 → 人工覆核 → 核准 → Phase II 監控** 的完整流程，
還可以呼叫 LLM 產生中文異常摘要。

兩條線共用同一個 PostgreSQL 資料庫（`REALDB`）與同一個本機 Ollama。

---

## 2. 系統架構

```mermaid
flowchart TD
    U["使用者瀏覽器"]

    subgraph RUN["常駐服務"]
        OW["Open WebUI<br/>open-webui<br/>host :3000<br/>內含 Aniki Pipe"]
        DASH["SPC Dashboard<br/>spc_dashboard<br/>Next.js 14<br/>host :3001"]
        PIPE["Pipelines FastAPI<br/>aniki_pipelines<br/>host :8000<br/>Aniki.py / metadatareader<br/>rowreader / generic_rowreader<br/>neon_db.py"]
        SPC["SPC API FastAPI<br/>spc_api<br/>不對外開埠<br/>spc-api:8000"]
        PG[("PostgreSQL 18<br/>REALDB<br/>realdb_postgres<br/>host :5431")]
        OL["Ollama<br/>host :11434<br/>qwen2.5vl:7b 辨識<br/>qwen2.5:7b 摘要"]
    end

    subgraph INIT["一次性初始化容器"]
        DBINIT["database-init<br/>還原 dump + 套 migrations"]
        MODINIT["ollama-model-init<br/>下載兩個模型"]
        PIPEINIT["aniki-openwebui-init<br/>安裝 Aniki Pipe"]
    end

    U -->|":3000 上傳檢查表"| OW
    U -->|":3001 看管制圖"| DASH

    OW -->|"HTTP + ANIKI_API_KEY"| PIPE
    PIPE -->|"VLM 辨識"| OL
    PIPE -->|"寫入辨識結果"| PG

    DASH -->|"讀規格 / 量測值 / 管制界線"| PG
    DASH -->|"SPC 計算 + AI 摘要"| SPC
    SPC -->|"產生中文摘要"| OL

    DBINIT -.->|"首次啟動"| PG
    MODINIT -.->|"pull 模型"| OL
    PIPEINIT -.->|"安裝並設為預設模型"| OW
```

另外還有三個**一次性初始化容器**，跑完就結束、狀態顯示 `Exited (0)`，這是正常的：

| 容器 | 工作 |
| --- | --- |
| `ollama_model_init` | 下載 `qwen2.5vl:7b`（辨識用）與 `qwen2.5:7b`（AI 摘要用），已存在就跳過 |
| `realdb_init` | REALDB 空的時候還原 `REALDB_backup.dump`，然後套用 `dashboard/db/migrations/*.sql` |
| `aniki_openwebui_init` | 把 `openwebui_aniki_pipe.py` 安裝／更新到 Open WebUI，啟用 Aniki 並設為預設模型 |

---

## 3. 服務與連接埠一覽

| 服務 | 容器名稱 | 主機連接埠 | 容器內連接埠 | 開啟網址 |
| --- | --- | --- | --- | --- |
| Open WebUI（辨識入口） | `open-webui` | **3000** | 8080 | <http://localhost:3000> |
| SPC Dashboard（管制圖） | `spc_dashboard` | **3001** | 3000 | <http://localhost:3001> |
| Pipelines API（Aniki） | `aniki_pipelines` | **8000** | 8000 | <http://localhost:8000/docs> |
| Ollama | `ollama` | **11434** | 11434 | <http://localhost:11434> |
| PostgreSQL | `realdb_postgres` | **5431** | 5432 | `localhost:5431`（用 DBeaver / psql 連） |
| SPC API（統計計算） | `spc_api` | *不對外* | 8000 | 只有容器內部 `http://spc-api:8000` 可達 |

> **為什麼 Dashboard 是 3001 不是 3000？**
> 3000 已經給 Open WebUI 用了。Dashboard 容器內部仍然跑在 3000，只是對外映射到 3001。
>
> **為什麼 PostgreSQL 是 5431 不是 5432？**
> 避免和電腦上可能已經安裝的本機 PostgreSQL 撞埠。

啟動前請確認 **3000 / 3001 / 8000 / 11434 / 5431** 這五個埠沒有被其他程式佔用。

---

## 4. 部署前的需求

### 4.1 硬體

| 項目 | 最低 | 建議 |
| --- | --- | --- |
| CPU | 4 核心 x86-64 | 8 核心以上 |
| 記憶體 | **16 GB** | 32 GB |
| 可用硬碟空間 | **30 GB** | 50 GB（SSD） |
| GPU | 非必要（純 CPU 可跑，辨識較慢） | NVIDIA GPU，8 GB VRAM 以上 |

空間主要花在：

- Ollama 模型約 **11 GB**（`qwen2.5vl:7b` ≈ 6 GB + `qwen2.5:7b` ≈ 4.7 GB）
- Docker 映像檔約 **8–10 GB**（open-webui、ollama、postgres、三個自建映像）
- 資料庫與 Open WebUI 資料 volume

> 記憶體低於 16 GB 時，視覺模型辨識一張 A4 檢查表可能要好幾分鐘，甚至因為記憶體不足失敗。

### 4.2 軟體

| 軟體 | 版本 | 備註 |
| --- | --- | --- |
| Windows | 10（21H2 以上）或 11 | 需支援 WSL2 |
| WSL2 | 最新 | Docker Desktop 的執行基礎 |
| Docker Desktop | 4.x 以上（含 Docker Compose v2） | **必要** |
| Git | 2.x | 用來 clone 這個 repo |

> 這套系統**完全跑在 Docker 裡**。你不需要在主機上另外安裝 Node.js、Python、PostgreSQL 或 Ollama。

### 4.3 網路

第一次啟動需要能連外網，用來：

- 下載 Docker 官方映像（`postgres:18`、`ollama/ollama`、`ghcr.io/open-webui/open-webui:main`、`node:20-alpine`、`python:3.12-slim`）
- `npm ci` 與 `pip install` 抓套件
- Ollama 下載兩個模型（約 11 GB）

跑起來之後，**辨識與 SPC 計算都在本機完成，不需要外部 API，也不會把資料送出去**。

---

## 5. 步驟一：安裝 WSL2 與 Docker Desktop

### 5.1 啟用 WSL2

以**系統管理員**身分開啟 PowerShell，執行：

```powershell
wsl --install
```

這個指令會一次做完：啟用「適用於 Linux 的 Windows 子系統」與「虛擬機器平台」功能、
安裝 WSL2 核心、安裝預設的 Ubuntu。完成後**重新開機**。

重開機後確認版本：

```powershell
wsl --status
wsl --list --verbose
```

`VERSION` 欄位要是 **2**。如果是 1，執行：

```powershell
wsl --set-default-version 2
```

> **常見問題：** 如果出現「無法啟用虛擬化」，請進 BIOS/UEFI 開啟
> Intel VT-x／AMD-V（通常叫 `Intel Virtualization Technology` 或 `SVM Mode`）。

### 5.2 安裝 Docker Desktop

1. 到 <https://www.docker.com/products/docker-desktop/> 下載 Windows 版安裝檔。
2. 安裝時**勾選** `Use WSL 2 instead of Hyper-V`。
3. 安裝完成後重新開機，啟動 Docker Desktop，等待左下角狀態變成綠色的 **Engine running**。
4. 開啟 Docker Desktop → **Settings → Resources**，確認：
   - Memory 至少 **12 GB**（WSL2 後端的話請改 `.wslconfig`，見下方）
   - Disk image size 至少 **40 GB**

WSL2 後端調整記憶體，是編輯 `C:\Users\<你的帳號>\.wslconfig`：

```ini
[wsl2]
memory=16GB
processors=8
swap=8GB
```

存檔後在 PowerShell 執行 `wsl --shutdown`，再重開 Docker Desktop。

驗證安裝：

```powershell
docker --version
docker compose version
docker run --rm hello-world
```

三個指令都要正常回應，`hello-world` 要印出 `Hello from Docker!`。

### 5.3（選用）啟用 NVIDIA GPU 加速

沒有 GPU 可以直接跳過這一節，系統會用 CPU 推論，只是比較慢。

1. 主機安裝最新的 **NVIDIA 驅動程式**（Game Ready 或 Studio 版皆可，需支援 WSL2）。
2. Docker Desktop → **Settings → Resources → WSL Integration** 打開你的 Ubuntu 發行版。
3. 測試 GPU 是否透得進容器：

   ```powershell
   docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
   ```

4. 若成功，編輯 `docker-compose.yml` 的 `ollama` 服務，加上 GPU 宣告：

   ```yaml
     ollama:
       image: ollama/ollama
       container_name: ollama
       restart: always
       ports:
         - "11434:11434"
       volumes:
         - ollama_data:/root/.ollama
       # ↓↓↓ 新增這一段 ↓↓↓
       deploy:
         resources:
           reservations:
             devices:
               - driver: nvidia
                 count: all
                 capabilities: [gpu]
       # ↑↑↑ 新增這一段 ↑↑↑
       healthcheck:
         test: ["CMD", "ollama", "list"]
         interval: 5s
         timeout: 5s
         retries: 30
       networks:
         - project_network
   ```

5. 改完後 `docker compose up -d ollama`，再用
   `docker compose exec ollama nvidia-smi` 確認容器內看得到顯示卡。

---

## 6. 步驟二：安裝 Git 並取得原始碼

### 6.1 安裝 Git

到 <https://git-scm.com/download/win> 下載安裝，一路 Next 即可。裝完確認：

```powershell
git --version
```

### 6.2 Clone 專案

選一個路徑（**建議放在純英文、沒有空白的資料夾**，例如 `C:\projects`）：

```powershell
mkdir C:\projects
cd C:\projects
git clone https://github.com/<你的帳號>/ipqc-spc-system.git
cd ipqc-spc-system
```

確認檔案都在：

```powershell
dir
```

應該看得到 `docker-compose.yml`、`init-realdb.sh`、`REALDB_backup.dump`、
`dashboard\`、`pipelines\`、`spc_model\`、`ipqc\`。

> **重要：`REALDB_backup.dump` 一定要存在。**
> 這是資料庫的 schema 與初始資料，少了它 `database-init` 會失敗，
> Dashboard 與 Pipelines 都起不來。
>
> **換行字元已由 `.gitattributes` 處理。** `init-realdb.sh` 要在 Linux 容器內執行，
> 一旦被轉成 CRLF 就會報 `no such file or directory`。repo 內的 `.gitattributes`
> 已經把所有 `.sh` 鎖成 LF，正常 clone 不會有問題。
> 萬一還是遇到，解法見 [疑難排解 14.7](#147-database-init-一直-restarting或報-no-such-file-or-directory)。

---

## 7. 步驟三：建立 `.env` 環境變數檔

`docker-compose.yml` 需要兩個**沒有預設值**的變數，必須自己建立。

最快的做法是直接複製範例檔：

```powershell
Copy-Item .env.example .env
```

然後編輯 `.env`。完整內容與說明如下：

```env
# ===========================================================================
#  專案根目錄 .env —— docker compose 會自動讀取這個檔
#  這個檔已經被 .gitignore 排除，不會被提交到 GitHub
# ===========================================================================

# --- 必填 ------------------------------------------------------------------

# PostgreSQL 的 postgres 使用者密碼。自己決定，但不要留空。
# 只能用英數與一般符號，避免 # $ 這類會被 shell 解讀的字元。
DB_PASSWORD=ChangeThisToYourOwnPassword

# Pipelines API 的存取金鑰。Open WebUI 的 Aniki Pipe 會拿這把 key 呼叫 API。
# 建議 32 字元以上的隨機英數字串。
ANIKI_API_KEY=please-replace-with-a-long-random-string-32chars

# --- 選填（不填就用下面這些預設值）-------------------------------------------

# 檢查表辨識用的視覺語言模型
VLM_MODEL=qwen2.5vl:7b

# SPC AI 摘要用的語言模型
SPC_OLLAMA_MODEL=qwen2.5:7b

# Dashboard 設定對話框（①區齒輪）的密碼。留空則使用程式內建預設值 spc1234。
SETTINGS_PASSWORD=
```

### 用 PowerShell 產生一把隨機 API Key

```powershell
-join ((48..57) + (65..90) + (97..122) | Get-Random -Count 48 | ForEach-Object {[char]$_})
```

把輸出貼到 `ANIKI_API_KEY=` 後面。

### 建立 `.env` 的注意事項

- 檔名就是 `.env`，**沒有副檔名**。用記事本存檔時，「存檔類型」要選「所有檔案」，
  否則會變成 `.env.txt`。建議用 VS Code 建立。
- `=` 左右**不要加空白**，值**不要加引號**。
- 不要把 `.env` 提交到 Git（`.gitignore` 已經排除了）。

驗證 compose 有讀到變數：

```powershell
docker compose config | findstr POSTGRES_PASSWORD
```

應該要看到你設定的密碼，而不是空白。

---

## 8. 步驟四：第一次啟動

確定 Docker Desktop 已經啟動（綠燈），在專案根目錄執行：

```powershell
docker compose up -d --build
```

這一行會做完所有事情：拉映像、建置三個自製映像、啟動容器、下載模型、還原資料庫、安裝 Aniki Pipe。

> **第一次啟動會很久。** 依網路速度，大約 **20 ～ 60 分鐘**。
> 主要時間花在下載 11 GB 的 Ollama 模型與 Docker 映像。指令本身會很快回到
> 命令提示字元（因為有 `-d`），但背景還在忙，請用 [8.3](#83-追蹤第一次啟動的進度)
> 的指令確認進度。

### 8.1（選用）網路太慢的話：從 Release 下載離線包

如果你遇到下面任何一種情況，可以跳過線上下載，改用預先打包好的離線包：

- `ghcr.io` 下載極慢或卡住不動（實測過的問題，見 [14.11](#1411-ghcrio-下載極慢或卡在-0-bytes)）
- 網路不穩，`docker compose up` 跑一半失敗要重來
- 目標機器**完全沒有外網**（工廠機台常見）
- 要一次裝好幾台，不想每台都重下一次

**離線包放在這個 repo 的 [Releases](../../releases) 頁面**，標籤是 `offline-*`。

#### 下載

Release 附件單檔上限 2 GB，所以大檔是切成分割檔上傳的。
**要把該 tag 底下的檔案全部下載到同一個資料夾**，一個都不能少：

```text
images.tar.gz.part00, part01, ...     所有 Docker 映像
ollama_models.tar.part00, ...         11 GB 模型（只有部分版本才有）
SHA256SUMS.txt                        校驗碼
manifest.json                         版本與內容清單
install-offline.ps1                   安裝腳本
```

用 GitHub CLI 一次抓完最省事：

```powershell
gh release download offline-v1.0.0 --dir E:undle
```

或到 Releases 頁面手動一個一個下載。

#### 安裝

```powershell
cd <專案資料夾>
E:\bundle\install-offline.ps1
```

腳本會驗證校驗碼 → 合併分割檔 → `docker load` → 還原模型 →
提示填 `.env` → `docker compose up -d`。

完成後直接跳到[步驟五驗證](#9-步驟五驗證部署是否成功)。

> **注意：離線包裡只有映像和模型，沒有原始碼。** compose 還需要
> `REALDB_backup.dump`、`init-realdb.sh`、`dashboard/db/migrations/`、
> `pipelines/openwebui_*.py`、`ipqc/` 這些檔案，所以[步驟二](#6-步驟二安裝-git-並取得原始碼)
> 還是要做——只是不用再等下載。
>
> 合併過程中會同時存在分割檔與合併後的大檔，**磁碟峰值需要約兩倍空間**。

製作離線包的方法（要發布新版時用）寫在 [OFFLINE-INSTALL.md](OFFLINE-INSTALL.md)。

### 8.2 啟動順序（compose 的依賴關係）

```text
postgres ──(healthy)──► database-init ──(completed)──┬──► dashboard
                                                     └──► pipelines
ollama ──(healthy)──► ollama-model-init ──(completed)──┬──► spc-api ──(healthy)──► dashboard
                                                       ├──► pipelines
                                                       └──► openwebui ──(healthy)──► aniki-openwebui-init
                                        pipelines ──(healthy)──► openwebui
```

也就是說：

- 資料庫沒 ready，`database-init` 不會跑；還原沒成功，Dashboard 與 Pipelines 不會啟動。
- 模型沒下載完，`spc-api`、`pipelines`、`openwebui` 都在等。
- Open WebUI 要等 `pipelines` healthy 才啟動；`aniki-openwebui-init` 又要等 Open WebUI healthy。

所以第一次啟動時，`docker compose ps` 會看到一堆 `Created` / `waiting`，**這是正常的**。

### 8.3 追蹤第一次啟動的進度

```powershell
# 看整體狀態
docker compose ps

# 看模型下載進度（最花時間的一步）
docker compose logs -f ollama-model-init

# 看資料庫還原
docker compose logs database-init

# 看 Aniki Pipe 有沒有裝進 Open WebUI
docker compose logs aniki-openwebui-init

# 全部服務的即時日誌
docker compose logs -f
```

模型下載完成時，`ollama-model-init` 的日誌最後會出現 `success`，容器狀態變成 `Exited (0)`。

資料庫第一次還原成功，`database-init` 會印出：

```text
REALDB is empty; restoring /backup/REALDB_backup.dump
REALDB restore completed
applying migration 20260904_phase_i_trial_review.sql
migrations completed
```

---

## 9. 步驟五：驗證部署是否成功

### 9.1 容器狀態

```powershell
docker compose ps
```

預期結果：

| 容器 | 預期狀態 |
| --- | --- |
| `realdb_postgres` | `Up (healthy)` |
| `ollama` | `Up (healthy)` |
| `spc_api` | `Up (healthy)` |
| `aniki_pipelines` | `Up (healthy)` |
| `open-webui` | `Up (healthy)` |
| `spc_dashboard` | `Up` |
| `realdb_init` | `Exited (0)` ← 跑完就結束，**正常** |
| `ollama_model_init` | `Exited (0)` ← 跑完就結束，**正常** |
| `aniki_openwebui_init` | `Exited (0)` ← 跑完就結束，**正常** |

> 三個 `_init` 容器顯示 `Exited (0)` 代表**初始化成功**，不是壞掉。
> 如果是 `Exited (1)` 或不斷 `Restarting`，才是有問題，請看該容器的日誌。

### 9.2 逐一測試每個服務

**① Ollama 模型**

```powershell
docker compose exec ollama ollama list
```

要看到 `qwen2.5vl:7b` 與 `qwen2.5:7b` 兩個模型。

**② PostgreSQL 資料表**

```powershell
docker compose exec postgres psql -U postgres -d REALDB -c "\dt"
```

要看到 **10 張**資料表：dump 還原的 9 張
`來源檔案`、`品號`、`製程`、`機台`、`工件`、`球標尺寸`、`測量值`、`事件紀錄`、`管制圖`，
加上 migration 建立的 `phase_i_trial_review`。

再確認兩個 view：

```powershell
docker compose exec postgres psql -U postgres -d REALDB -c "\dv"
```

要看到 `事件使用區間`、`工件_含事件`。

**③ Pipelines API 健康檢查**

瀏覽器開 <http://localhost:8000/health>，預期：

```json
{
  "status": "ok",
  "aniki_exists": true,
  "env_exists": false,
  "api_key_configured": true,
  "input_dir": "/app/ipqc",
  "input_dir_exists": true
}
```

- `api_key_configured` 若是 `false`，代表 `.env` 的 `ANIKI_API_KEY` 沒吃到，回去看 [步驟三](#7-步驟三建立-env-環境變數檔)。
- `env_exists` 在 Docker 裡是 `false` **屬於正常**——容器裡沒有 `pipelines/.env` 檔，
  環境變數是 compose 直接注入的。只有單獨跑 Python 時才需要那個檔。

**④ SPC API 健康檢查**（沒有對外埠，要從容器內打）

```powershell
docker compose exec spc-api python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/health').read())"
```

預期 `b'{\"status\":\"ok\"}'`。

**⑤ Dashboard 資料庫連線診斷**

瀏覽器開 <http://localhost:3001/api/db-info>，應該回傳資料庫名稱與連線資訊，
而不是錯誤訊息。再開 <http://localhost:3001/api/schema-check> 確認必要的表／view 都在。

**⑥ 兩個網頁**

- Open WebUI：<http://localhost:3000>
- SPC Dashboard：<http://localhost:3001>

兩個都要能開得出畫面。

---

## 10. 步驟六：第一次使用

### 10.1 Open WebUI 建立管理員帳號

1. 開 <http://localhost:3000>。
2. 第一次進入會要求**註冊**。**第一個註冊的帳號自動成為管理員。**
3. 電子郵件與密碼自己決定（這是本機資料庫，不會寄信，隨便一個 email 格式即可）。
4. 請自己記住密碼，忘記的話最快的解法是清掉 `openwebui_data` volume 重來
   （會一併清掉聊天紀錄）。

### 10.2 用 Aniki 辨識一張檢查表

1. 開一個**新對話**。
2. 左上角的模型應該已經是 **`aniki`**（`aniki-openwebui-init` 已經把它設成預設）。
   若不是，手動從下拉選單選 `aniki`。
3. 點輸入框旁的 **＋** 上傳一份 PDF 或圖片。
   repo 的 `ipqc/` 資料夾裡有 20 多份真實檢查表可以直接拿來測，例如 `檢表5.pdf`。
4. 訊息內容**可以留空或隨便打**，不需要下特定指令，直接送出。
5. 等待辨識。第一次因為要載入模型，會比較久（CPU 推論可能 3～10 分鐘／頁）。
6. 完成後 Aniki 會回覆辨識結果摘要與資料庫寫入結果。

**同名檔案的處理：** 如果資料庫裡已經有同檔名的資料，Aniki 會先問你要不要用新資料取代。
確認後才會重新辨識並更新；取消則保留舊資料。若瀏覽器沒跳出確認視窗，直接在對話框
回覆「取代」或「取消」即可。

### 10.3 人工確認頁面

Pipelines 容器裡另外掛了一個**人工確認模組**（`pipelines/app.py`），和辨識 API 共用
同一個 FastAPI 與同一個 `8000` 埠，不需要另外開服務。

它是一個**獨立的模組**：`POST /process` 辨識完並不會自動幫你開確認任務，
確認任務要由呼叫端用 `POST /api/reviews` 建立，回傳的 `review_url` 就是確認頁網址：

```text
http://localhost:8000/review/<token>
```

在這個頁面可以逐格修改辨識結果、新增／刪除列與欄，按確認後保存結果。
確認狀態存在 `pipelines_data` volume 裡的 SQLite（`/app/data/review_tasks.db`），
容器重啟不會消失。

想先看看這個頁面長什麼樣，直接開 <http://localhost:8000/review-demo/create>
會產生一筆示範資料並回傳 `review_url`，把那個網址貼到瀏覽器即可。

> `review_url` 是用**收到請求時的 Host** 組出來的。從瀏覽器打 `localhost:8000` 產生的
> 連結就是 `http://localhost:8000/...`；如果是從別的容器內部呼叫，會變成
> `http://pipelines:8000/...`，那個網址在瀏覽器打不開，把主機名稱換成 `localhost` 即可。

### 10.4 直接用 Swagger 測 API（不透過 Open WebUI）

開 <http://localhost:8000/docs>，展開 **`POST /process`**，按 `Try it out`：

| 欄位 | 填什麼 |
| --- | --- |
| `x-api-key` | 和 `.env` 的 `ANIKI_API_KEY` 完全一樣 |
| `file` | 選一份 PDF 或圖片 |
| `pages` | 留空＝全部頁面；也可以填 `1` 或 `1,2,3` |
| `replace_existing` | 第一次測選 `false` |
| `request_id` | 隨便給一個，例如 `test-001` |

成功會回傳：

```json
{
  "success": true,
  "neon_success": true,
  "file_name": "檢表5.pdf",
  "row_count": 17
}
```

> `neon_success` 是舊版留下來的欄位名稱，現在代表「是否成功寫入本機 PostgreSQL」。

### 10.5 SPC Dashboard 的操作流程

開 <http://localhost:3001>。畫面固定為一個螢幕高度、不捲動，分成五個彩色區塊：

| 區塊 | 顏色 | 內容 |
| --- | --- | --- |
| ① 選擇 | 灰 | 品號／製程／機台／事件區間／管制圖類型的下拉選單、門檻值、設定齒輪 |
| ② 製程能力 | 藍 | 所有球標尺寸的 Cpk 表格、選定尺寸的能力指標、每日 X-bar 趨勢 |
| ③ 管制監控 | 綠 | 上圖（I／Xbar）與下圖（MR／R／S）並排，可切換監控／分析模式 |
| ④ 異常判定 | 琥珀 | 判定統計與異常點清單（上圖藍色、下圖紫色） |
| ⑤ 決策 | 紫 | Phase I 試算／核准按鈕、AI 摘要 |

**Phase I → Phase II 的完整流程：**

1. 在 ① 區依序選擇品號、製程、機台、事件類型、事件區間、管制圖類型。
2. 在 ② 區的 Cpk 表格點一列，選定要分析的球標尺寸。
3. 如果這個尺寸還沒有生效中的管制界線，③ 區會顯示「**尚未建立管制界線**」，
   ⑤ 區出現 **`Phase I 試算`** 按鈕。
4. 按 **`Phase I 試算`**：系統讀取歷史量測值、算出試算界線，**但不寫入資料庫**。
   疑似異常點會列出來，每個點都有排除用的勾選框。
5. 覆核每個疑似異常點：勾選要排除的點，填寫排除原因（原因選 `other` 時必須補充說明，
   這是資料庫 `CHECK` 約束強制的稽核要求）。
6. 勾選後按重算，系統用扣掉排除點的樣本重新試算。
7. 確認新的試算圖沒問題後，按 **`核准並進入 Phase II`**。
   伺服器會**重新計算一次**（不信任前端傳來的數值），寫入一筆新的管制圖版本，
   之後這個區間就進入 Phase II 監控。

**自動建立管制界線（選用）：** 在 ① 區的齒輪打開設定對話框（密碼預設 `spc1234`，
可用 `SETTINGS_PASSWORD` 覆寫），把 `auto_create_control_limit` 打開之後，
系統只會在「**首次試算 + 沒有排除任何點 + 上下兩張圖都沒有疑似異常點**」時自動核准。
一旦進入人工排除流程，仍然必須手動核准。

**AI 摘要：** ⑤ 區的 AI 摘要按鈕會把目前圖上的資料送到 `spc-api`，
由 Ollama 的 `qwen2.5:7b` 產生一段中文異常說明。第一次呼叫要載入模型，會比較慢。

> **注意：Dashboard 的三個設定（`min_samples`、`cpk_threshold`、`auto_create_control_limit`）
> 存在 Node 程式的記憶體裡，容器重啟就會回到預設值**（5 / 1.33 / false）。
> 這是已知限制，需要持久化的話要改存資料庫。

---

## 11. 環境變數完整說明

### 11.1 根目錄 `.env`（你唯一需要建立的檔案）

| 變數 | 必填 | 預設 | 用途 |
| --- | --- | --- | --- |
| `DB_PASSWORD` | ✅ | 無 | PostgreSQL `postgres` 使用者的密碼。同時被 `postgres`、`database-init`、`dashboard`、`pipelines` 使用 |
| `ANIKI_API_KEY` | ✅ | 無 | Pipelines API 的存取金鑰，Open WebUI 的 Aniki Pipe 用它呼叫 API |
| `VLM_MODEL` | ⬜ | `qwen2.5vl:7b` | 檢查表辨識用的視覺語言模型 |
| `SPC_OLLAMA_MODEL` | ⬜ | `qwen2.5:7b` | `ollama-model-init` 要額外下載的模型 |
| `SETTINGS_PASSWORD` | ⬜ | `spc1234` | Dashboard 設定對話框的密碼 |

> ✅ `SPC_OLLAMA_MODEL` 現在同時決定 `ollama-model-init` **下載**哪個模型、
> 以及 `spc-api` 實際**呼叫**哪個模型，兩邊不會再對不上。
> （舊版把 `spc-api` 的 `OLLAMA_MODEL` 寫死成 `qwen2.5:7b`，改 `.env` 只會下載新模型，
> AI 摘要仍然去呼叫舊的然後失敗。）

### 11.2 各服務實際收到的環境變數（由 compose 注入，不需要手動設定）

**`dashboard`（Next.js）**

| 變數 | 值 | 說明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:${DB_PASSWORD}@postgres:5432/REALDB` | 容器間用**服務名稱 + 內部埠**，不是 localhost 也不是 5431 |
| `SPC_API_BASE` | `http://spc-api:8000` | 所有統計判定都在這裡算，連不上就畫不出圖 |
| `SETTINGS_PASSWORD` | 來自 `.env` | 設定對話框密碼 |
| `PGPOOL_MAX` | 未設定（預設 10） | 連線池上限，可自行加入 |

**`pipelines`（FastAPI / Aniki）**

| 變數 | 值 |
| --- | --- |
| `DATABASE_URL` | `postgresql://postgres:${DB_PASSWORD}@postgres:5432/REALDB` |
| `ANIKI_API_KEY` | 來自 `.env` |
| `OLLAMA_URL` | `http://ollama:11434/api/chat` |
| `VLM_MODEL` / `VLM_RECHECK_MODEL` | 來自 `.env`，預設 `qwen2.5vl:7b` |
| `OLLAMA_KEEP_ALIVE` | `10m`（模型在記憶體停留多久，避免每次重載） |
| `REVIEW_DB_PATH` | `/app/data/review_tasks.db`（人工確認的 SQLite） |
| `ANIKI_INPUT_DIR` | `/app/ipqc`（對應主機的 `./ipqc`） |

**`spc-api`（Python SPC）**

| 變數 | 值 |
| --- | --- |
| `OLLAMA_BASE_URL` | `http://ollama:11434` |
| `OLLAMA_MODEL` | `qwen2.5:7b` |

**`openwebui`**

| 變數 | 值 |
| --- | --- |
| `OLLAMA_BASE_URL` | `http://ollama:11434` |
| `ENABLE_PLUGINS` | `true` |
| `DEFAULT_MODELS` | `aniki` |
| `ANIKI_API_URL` | `http://pipelines:8000` |
| `ANIKI_API_KEY` | 來自 `.env` |

> 各子資料夾底下的 `.env.example`（`dashboard/`、`pipelines/`）是**單獨執行該服務時**才用得到的範例。
> 用 `docker compose` 整套啟動時，**只需要根目錄那一份 `.env`**。

---

## 12. 資料庫說明

### 12.1 Schema

`REALDB_backup.dump` 是用 **PostgreSQL 18.3** 匯出的 custom format dump，
裡面有 **9 張表與 2 個 view**；第 10 張表 `phase_i_trial_review` 不在 dump 裡，
是由 `dashboard/db/migrations/` 的 migration 建立的。

**資料表**

| 表 | 主鍵 | 說明 |
| --- | --- | --- |
| `來源檔案` | — | 已辨識過的檢查表檔案紀錄（用來擋同名檔案） |
| `品號` | — | 產品品號主檔 |
| `製程` | — | 製程主檔 |
| `機台` | — | 機台主檔 |
| `工件` | `(機台, 流水號)` | 一件工件一列 |
| `球標尺寸` | — | 尺寸規格（名義值、上下公差） |
| `測量值` | `(機台, 流水號, 球標尺寸名)` | 實際量測值，含 `是否異常` 旗標 |
| `事件紀錄` | `id` | 換刀／保養／參數調整等事件（`事件類型` 是自由文字） |
| `管制圖` | `(品號, 製程, 機台, 球標尺寸名, 管制圖類型, 管制開始時間)` | 管制界線版本與製程能力值 |
| `phase_i_trial_review` | 複合鍵 | Phase I 疑似異常點的人工覆核紀錄（由 migration 建立） |

**View**

| View | 說明 |
| --- | --- |
| `事件使用區間` | 用 `LEAD()` 推算每個事件的流水號區間（`結束流水號_不含`） |
| `工件_含事件` | 每個工件預先對應到它所屬的事件區間；**一個工件會對應到每一種事件類型各一列** |

> ⚠️ 這兩個 view 是管制圖分區間的基礎，**建資料庫時一定要一併建立**。
> 更多 schema 與查詢細節寫在 [`dashboard/HANDOFF.md`](dashboard/HANDOFF.md)。

### 12.2 初始化邏輯（`init-realdb.sh`）

`database-init` 容器每次啟動都會跑一次這個腳本：

| 情況 | 行為 |
| --- | --- |
| REALDB 完全空白（0 張表） | 還原 `REALDB_backup.dump` |
| 已有資料表，而且 `來源檔案` 存在 | **跳過還原**，不覆蓋既有資料 |
| 有資料表但缺 `來源檔案` | **中止並回報錯誤**，不自動清除資料 |

不論走哪一條，最後都會把 `dashboard/db/migrations/*.sql` 全部重跑一次。
因為**沒有版本追蹤表**，所以每一個 migration 腳本都必須是**冪等**的
（`CREATE TABLE IF NOT EXISTS`、`CREATE INDEX IF NOT EXISTS`、`DO $$ ... $$` 條件判斷）。
新增 migration 時請維持這個原則。

### 12.3 PostgreSQL 版本要求

dump 是 **PostgreSQL 18.3** 產生的（dump 檔頭版本 1.16）。
`docker-compose.yml` 用的是 `postgres:18`，**不能降版**。
用 `postgres:16` 或更舊的映像，`pg_restore` 會直接報
`unsupported version (1.16) in file header` 而失敗。

### 12.4 手動連線

用 psql（容器內）：

```powershell
docker compose exec postgres psql -U postgres -d REALDB
```

用 DBeaver / pgAdmin（主機）：

| 欄位 | 值 |
| --- | --- |
| Host | `localhost` |
| Port | `5431` |
| Database | `REALDB` |
| User | `postgres` |
| Password | 你在 `.env` 設的 `DB_PASSWORD` |

### 12.5 備份與還原

**備份目前的資料庫：**

```powershell
docker compose exec postgres pg_dump -U postgres -d REALDB -Fc -f /tmp/backup.dump
docker compose cp postgres:/tmp/backup.dump .\my_backup.dump
```

**還原到乾淨的資料庫：**

```powershell
docker compose cp .\my_backup.dump postgres:/tmp/restore.dump
docker compose exec postgres pg_restore -U postgres -d REALDB --clean --no-owner --no-privileges /tmp/restore.dump
```

### 12.6 資料存在哪裡（Docker volumes）

| Volume | 內容 | 刪掉會怎樣 |
| --- | --- | --- |
| `real-project_pgdata` | PostgreSQL 全部資料 | **所有辨識結果與管制圖版本消失**，下次啟動重新還原 dump |
| `real-project_ollama_data` | Ollama 模型 | 要重新下載 11 GB |
| `real-project_openwebui_data` | Open WebUI 帳號、聊天紀錄、已安裝的 Aniki Pipe | 要重新註冊管理員帳號 |
| `real-project_pipelines_data` | 人工確認的 SQLite | 人工確認紀錄消失 |

查看：

```powershell
docker volume ls | findstr real-project
```

> compose 專案名稱在 `docker-compose.yml` 第一行定為 `real-project`，
> 所以 volume 前綴固定是 `real-project_`，跟資料夾名稱無關。

`./ipqc` 是**主機資料夾的 bind mount**，不是 volume。上傳到 Open WebUI 的檔案會
存進這裡，直接在檔案總管就看得到。

### 12.7 Ollama 模型存在哪裡

這是最常被問的一題，因為那 11 GB 找不到人。

| 層次 | 位置 |
| --- | --- |
| 容器內路徑 | `/root/.ollama`（Ollama 預設；本專案沒有覆寫 `OLLAMA_MODELS`） |
| Docker volume | `real-project_ollama_data` |
| Windows 實體檔案 | 在 WSL2 的虛擬磁碟裡：`%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx`（未改過位置時） |

volume 內部結構：

```text
/root/.ollama/
├─ models/
│  ├─ blobs/          ← 真正的權重檔，11 GB 幾乎都在這
│  └─ manifests/registry.ollama.ai/library/
│     ├─ qwen2.5vl/7b
│     └─ qwen2.5/7b
├─ id_ed25519
└─ id_ed25519.pub
```

**它不在專案資料夾，也不在 `C:\Users\你\.ollama`**（那是原生安裝 Ollama 才會用的路徑）。
Docker volume 住在 WSL2 的 vhdx 內部，檔案總管打不開。

檢查：

```powershell
docker system df -v | findstr ollama_data
docker compose exec ollama sh -c "du -sh /root/.ollama/models"
docker compose exec ollama ollama list
```

### 12.8 備份與還原 Ollama 模型

重灌、搬機器、或交付到廠內那台時，這一步能省掉 11 GB 的下載——在網路不穩的
環境裡差別非常大。

```powershell
# 匯出（預設寫到 E:\docker-backup\ollama_models.tar）
.\scripts\ollama-models.ps1 -Action export

# 指定位置
.\scripts\ollama-models.ps1 -Action export -Path D:\backup\models.tar

# 還原
.\scripts\ollama-models.ps1 -Action import
```

macOS / Linux：

```bash
./scripts/ollama-models.sh export
./scripts/ollama-models.sh import
```

還原之後 `docker compose up -d`，`ollama-model-init` 會發現模型已存在而跳過下載。

> **tar 檔不要放在 repo 資料夾裡。** 11 GB 的檔案誤 commit 會很麻煩。
> `.gitignore` 已經擋掉 `*.tar`，但放在 repo 外面更保險。
> 也不要放 C 槽——那通常正是你想騰出空間的那一顆。

---

## 13. 日常維運指令

所有指令都在**專案根目錄**執行。

```powershell
# 啟動（背景）
docker compose up -d

# 啟動並重新建置自製映像（改過程式碼時用）
docker compose up -d --build

# 停止（保留資料）
docker compose stop

# 停止並移除容器（保留 volume，資料還在）
docker compose down

# ⚠️ 停止並移除容器 + 所有資料（模型、資料庫、帳號全部清空）
docker compose down -v

# 看狀態
docker compose ps

# 看日誌
docker compose logs -f                  # 全部
docker compose logs -f dashboard        # 單一服務
docker compose logs --tail 100 pipelines

# 重啟單一服務
docker compose restart dashboard

# 只重建某一個服務
docker compose up -d --build dashboard

# 進容器內的 shell
docker compose exec pipelines sh
docker compose exec postgres bash

# 查看資源用量
docker stats
```

### 更新程式碼之後

```powershell
git pull
docker compose up -d --build
```

`--build` 會重新建置 `dashboard`、`pipelines`、`spc-api` 三個自製映像。
資料庫與模型不受影響（`database-init` 會偵測到 REALDB 已初始化而跳過還原，
只重跑 migrations）。

### 重新安裝 Aniki Pipe 到 Open WebUI

```powershell
docker compose up -d --force-recreate aniki-openwebui-init
docker compose logs aniki-openwebui-init
```

這個初始化容器是可重複執行的：每次都會確認 Aniki 已安裝、已啟用、且更新到 repo 內的版本，
既有聊天紀錄與手動調整過的 Valves 都會保留。

### 手動管理 Ollama 模型

```powershell
docker compose exec ollama ollama list
docker compose exec ollama ollama pull qwen2.5vl:7b
docker compose exec ollama ollama rm <模型名稱>
```

---

## 14. 疑難排解

### 14.1 `docker compose up` 報 `variable is not set`

```text
WARN[0000] The "DB_PASSWORD" variable is not set. Defaulting to a blank string.
```

根目錄沒有 `.env`，或是檔名被記事本存成 `.env.txt`。
回到 [步驟三](#7-步驟三建立-env-環境變數檔) 重建，並用
`docker compose config` 確認變數有被代入。

### 14.2 埠被佔用（`port is already allocated`）

先找出誰佔用：

```powershell
netstat -ano | findstr ":3000 :3001 :8000 :11434 :5431"
```

用最後一欄的 PID 到工作管理員找出程式。若無法關閉，就改 `docker-compose.yml`
的主機埠（**冒號左邊**那個數字），例如把 Dashboard 改成 `"3002:3000"`。
**不要改冒號右邊**，那是容器內部埠，改了服務就連不到彼此。

### 14.3 `database-init` 顯示 `Exited (1)`

看日誌：

```powershell
docker compose logs database-init
```

常見原因：

| 日誌訊息 | 原因與解法 |
| --- | --- |
| `unsupported version (1.16) in file header` | PostgreSQL 版本太舊。確認 `docker-compose.yml` 用的是 `postgres:18` |
| `REALDB contains tables but the required 來源檔案 table is missing` | 資料庫處於半初始化狀態。備份重要資料後執行 `docker compose down -v` 重來 |
| `No such file or directory` | `REALDB_backup.dump` 不存在，或 `init-realdb.sh` 是 CRLF 換行（見 14.7） |
| `password authentication failed` | `.env` 的 `DB_PASSWORD` 改過，但 `pgdata` volume 還留著舊密碼。執行 `docker compose down -v` 重建，或進資料庫改密碼 |

### 14.4 模型下載卡住或失敗

> 先看 [14.12](#1412-docker-compose-up-停在-waiting-不動)——`compose up` 停在
> `Waiting` 通常不是卡住，是模型正在下載但進度沒顯示出來。

```powershell
docker compose logs -f ollama-model-init
```

下載中斷時，`restart: on-failure` 會自動重試，而且 Ollama 會沿用已下載的 layer，
不會從頭來過。如果一直失敗：

```powershell
docker compose up -d --force-recreate ollama-model-init
```

或者手動拉：

```powershell
docker compose exec ollama ollama pull qwen2.5vl:7b
docker compose exec ollama ollama pull qwen2.5:7b
```

### 14.5 Open WebUI 裡沒有 `aniki` 模型

```powershell
docker compose logs aniki-openwebui-init
docker compose up -d --force-recreate aniki-openwebui-init
```

重跑之後**重新整理瀏覽器**（Ctrl + F5）。
還是沒有的話，到 Open WebUI 的 **Workspace → Functions** 看 `aniki` 是不是被停用了。

### 14.6 辨識時回報 `API key 錯誤`（HTTP 401）

Open WebUI 的 Aniki Pipe 拿到的 key 和 Pipelines 的不一樣。

1. 確認 `.env` 的 `ANIKI_API_KEY` 有值。
2. `docker compose up -d` 讓兩個容器都吃到新的值。
3. 在 Open WebUI 的 **Workspace → Functions → aniki → Valves**，
   確認 `API_KEY` 欄位與 `.env` 一致（手動改過 Valves 的話，環境變數不會覆蓋它）。

### 14.7 `database-init` 一直 `Restarting`，或報 `no such file or directory`

**這是實際發生過、最花時間的一個坑。**

典型症狀是 `docker compose up` 永遠停在：

```text
- Container realdb_init  Waiting     768.1s
```

而 `docker compose ps -a` 顯示：

```text
realdb_init   postgres:18   ...   Restarting (2) 11 seconds ago
```

**退出碼 2 = psql 連線失敗。** `restart: on-failure` 讓它不斷重啟，
compose 的 `service_completed_successfully` 就永遠等不到，
`pipelines` / `openwebui` / `dashboard` 全部卡在 `Created`。

根因幾乎都是 **Windows CRLF 換行**。Windows 版 Git 預設 `core.autocrlf=true`，
`init-realdb.sh` 每一行結尾會多一個 `\r`，於是

```sh
PSQL="psql --host=postgres --username=postgres --dbname=REALDB"
```

變成 `--dbname=REALDB\r`，psql 去找一個叫 `REALDB\r` 的資料庫 → 連不上 → exit 2。

> repo 內的 `.gitattributes` 已經把所有 `.sh` 鎖成 LF，正常 clone 不會有這個問題。
> 這一節是給「從舊版複製過來」或「.gitattributes 被移除」的情況用的。

**先確認是不是這個原因：**

```powershell
foreach ($f in @("init-realdb.sh", ".env")) {
  $b = [System.IO.File]::ReadAllBytes("$PWD\$f")
  "{0,-20} {1}" -f $f, $(if ($b -contains 13) { "含 CR (CRLF) ← 有問題" } else { "純 LF 正常" })
}
```

`.env` 也要檢查——它若是 CRLF，`DB_PASSWORD` 的值結尾會多一個 `\r`，
那是另一種造成 exit 2 的方式。

**修正：**

```powershell
foreach ($f in @("init-realdb.sh", ".env")) {
  $p = "$PWD\$f"
  $t = [System.IO.File]::ReadAllText($p) -replace "`r`n", "`n"
  [System.IO.File]::WriteAllText($p, $t, (New-Object System.Text.UTF8Encoding $false))
}

docker compose up -d --force-recreate database-init
docker compose logs -f database-init
```

成功會看到：

```text
REALDB is empty; restoring /backup/REALDB_backup.dump
REALDB restore completed
applying migration 20260904_phase_i_trial_review.sql
CREATE TABLE
CREATE INDEX
DO
migrations completed
```

看到 `migrations completed` 就結束了。**`logs -f` 會繼續掛著等新日誌，
畫面不動是正常的**，按 `Ctrl+C` 離開，然後 `docker compose up -d` 把其餘服務帶起來。

**也可以用 VS Code 改：** 開啟 `init-realdb.sh`，點右下角狀態列的 `CRLF`
改成 **`LF`**，存檔，再 `docker compose up -d --force-recreate database-init`。

**解法二（一勞永逸）：** 改設定後重新 clone。

```powershell
git config --global core.autocrlf input
cd ..
rmdir /s /q PACKAGE
git clone https://github.com/<你的帳號>/ipqc-spc-system.git
cd ipqc-spc-system
```

（`.env` 記得重新建立。）

**驗證換行是否正確：**

```powershell
docker compose run --rm --entrypoint sh database-init -c "head -c 20 /opt/init-realdb.sh | od -c | head -2"
```

輸出裡只應該有 `\n`，不應該出現 `\r`。

### 14.8 Dashboard 開得起來但圖出不來

依序檢查：

1. <http://localhost:3001/api/db-info> — 資料庫連得上嗎？
2. <http://localhost:3001/api/schema-check> — 兩個 view 都建好了嗎？
3. `docker compose logs spc-api` — SPC 服務有沒有錯誤？
4. `docker compose logs dashboard` — 前端 API route 有沒有回 502？

最常見的原因是**資料庫裡根本還沒有量測資料**。先用 Open WebUI 或
`POST /process` 辨識幾張檢查表再回來看。

### 14.9 辨識非常慢或失敗

- **純 CPU 推論本來就慢**，一張 A4 檢查表 3～10 分鐘是正常的。
- 記憶體不足時 Ollama 會直接失敗。檢查 `docker stats` 與 Docker Desktop 的
  Memory 設定（見 [5.2](#52-安裝-docker-desktop)）。
- 有 NVIDIA GPU 的話啟用 GPU 加速（見 [5.3](#53選用啟用-nvidia-gpu-加速)），差距很大。
- 圖片太糊可以調整 `pipelines/Aniki.py` 的 `PDF_DPI`（預設 150，可改 180 或 200）；
  記憶體吃緊就往下調到 120。改完要 `docker compose up -d --build pipelines`。

### 14.10 磁碟空間不足

```powershell
docker system df          # 看用掉多少
docker system prune -a    # ⚠️ 清掉所有未使用的映像與快取（不會動到 volume）
```

### 14.11 `ghcr.io` 下載極慢或卡在 0 bytes

**這是實際遇到過的問題，而且不是你機器的錯。**

症狀：`docker pull` Docker Hub 的 image（例如 `python:3.12-slim`）十秒內完成，
但 `ghcr.io/open-webui/open-webui:main` 跑幾百秒還停在 0 bytes 或個位數 MB。

先確認確實是 ghcr 而不是你這邊：

```powershell
# 主機頻寬
Measure-Command { curl.exe -o NUL -s https://speed.cloudflare.com/__down?bytes=50000000 }

# Docker Hub 的 image 多快
docker rmi python:3.12-slim -f
Measure-Command { docker pull python:3.12-slim }
```

主機頻寬正常、Docker Hub 的 image 也快，只有 ghcr 慢 → 就是 ghcr 的問題。
GitHub 社群長期有這個回報，跟你的網路、防毒、CPU 都無關。

**解法，由易到難：**

1. **換 DNS** — CDN 依 DNS 結果分配節點，換一個常常會拿到不同 POP。
   Docker Desktop → Settings → Docker Engine：

   ```json
   {
     "max-concurrent-downloads": 1,
     "dns": ["1.1.1.1", "8.8.8.8"]
   }
   ```

2. **單獨 pull，不要用 `compose up` 一次拉四顆** — 並行下載會互相搶頻寬並逾時重試：

   ```powershell
   docker pull postgres:18
   docker pull ollama/ollama
   docker pull ghcr.io/open-webui/open-webui:main
   docker pull python:3.12-slim
   docker compose up -d --build
   ```

   中斷了直接重跑同一行，**已完成的 layer 會保留**，只有中斷當下那一層重來。

3. **換網路試一次** — 手機熱點三十秒就能判斷是不是 ISP 到 GitHub CDN 的路由問題。

4. **在別的機器拉，USB 搬過來**（最可靠）：

   ```powershell
   # 網路順的機器
   docker pull ghcr.io/open-webui/open-webui:main
   docker save ghcr.io/open-webui/open-webui:main -o openwebui.tar

   # 目標機器
   docker load -i openwebui.tar
   ```

> Docker Hub 上有非官方的 open-webui 鏡像。**官方只發佈在 ghcr.io**，
> 真要用第三方鏡像，先比對 digest 確認內容一致再用：
> `docker manifest inspect <image> | Select-String digest`

### 14.12 `docker compose up` 停在 `Waiting` 不動

大多數情況**這是正常的**，不是卡住。

```text
- Container ollama_model_init  Waiting     685.6s
```

`Waiting` 的意思是 compose 在等那個容器**執行完畢**（因為別的服務宣告了
`service_completed_successfully`）。`ollama_model_init` 此刻正在下載 11 GB 的模型，
而 compose 的進度列不會顯示容器內部的下載進度。

**看真實進度：**

```powershell
docker compose logs -f ollama-model-init

# 或看模型資料夾長多大，每分鐘應該往上跳
docker compose exec ollama sh -c "du -sh /root/.ollama/models"
```

**怎麼分辨真的卡住了：**

```powershell
docker compose ps -a
```

| 狀態 | 意義 |
| --- | --- |
| `Exited (0)` | 成功，後面的服務會自動接著起來 |
| `Up` 但 `du -sh` 的數字 5 分鐘不動 | 下載卡住，`docker compose up -d --force-recreate ollama-model-init` |
| `Restarting (n)` | **真的失敗了**，看 logs 找原因（`database-init` 見 [14.7](#147-database-init-一直-restarting或報-no-such-file-or-directory)） |

前景那個 `docker compose up` 可以直接 `Ctrl+C`，容器在背景繼續跑。

### 14.13 Docker Desktop 搬移磁碟位置失敗

C 槽空間不足想把 Docker 資料搬到別的磁碟時，
**Settings → Resources → Advanced → Disk image location** 有時會直接顯示失敗。

**先試最簡單的：** 托盤圖示 → Quit Docker Desktop（完全退出，不是關視窗），
然後 `wsl --shutdown`，再重試 GUI。大部分的失敗都是因為 vhdx 還被鎖著。

**不要手動搬 vhdx。** `docker_data.vhdx` 搬過去、改設定檔的 `DataFolder` 之後，
`docker-desktop` 這個 WSL distro 的註冊路徑仍記在登錄檔裡沒有跟著改，
Docker 會一邊從舊位置開 distro、一邊到新位置找資料磁碟，結果就是**永遠停在 starting**。
（backend log 會出現 `still waiting for init control API to respond`。）

**可靠的做法是重建：**

1. 備份模型與資料庫（[12.8](#128-備份與還原-ollama-模型) 與 [12.5](#125-備份與還原)）
2. 解除安裝 Docker Desktop，清掉殘留：

   ```powershell
   Stop-Service com.docker.service -Force -EA SilentlyContinue
   Get-Process "Docker Desktop","com.docker.backend","com.docker.build","docker-sandbox" `
       -EA SilentlyContinue | Stop-Process -Force
   wsl --shutdown
   Start-Sleep -Seconds 10

   wsl -l -v                        # 先看清楚有哪些 distro
   wsl --unregister docker-desktop  # ⚠️ 只打 docker-desktop，不要碰其他 distro

   foreach ($p in @("$env:LOCALAPPDATA\Docker","$env:APPDATA\Docker","$env:APPDATA\Docker Desktop",
                    "$env:USERPROFILE\.docker","$env:ProgramData\Docker","$env:ProgramData\DockerDesktop")) {
     if (Test-Path $p) { Remove-Item $p -Recurse -Force }
   }
   ```

   `docker_data.vhdx` 刪不掉、報「正由另一個處理序使用」時，直接重開機再刪。

3. 重新安裝 Docker Desktop
4. **第一次啟動後、拉任何 image 之前**，先到 Settings → Resources → Advanced
   把位置設到目標磁碟（此時完全沒有資料，必定成功）
5. 還原模型與資料庫

驗證真的搬過去了：

```powershell
Get-ChildItem "<新位置>" -Recurse -Filter *.vhdx |
  Select-Object FullName, @{n='GB';e={[math]::Round($_.Length/1GB,2)}}

Get-ChildItem "HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Lxss" |
  ForEach-Object { Get-ItemProperty $_.PSPath } |
  Select-Object DistributionName, BasePath | Format-Table -AutoSize
```

`docker-desktop` 的 `BasePath` 要指向新位置。

### 14.14 下載慢，但不是 ghcr 的問題

依序排除（每一項都能單獨驗證）：

| 嫌疑 | 怎麼查 | 怎麼修 |
| --- | --- | --- |
| 防毒即時掃描 vhdx | `(Get-MpPreference).ExclusionPath` 裡有沒有 Docker 的資料路徑 | 見下方 |
| 系統 proxy | `docker info` 的 `HTTP Proxy` 行；`netsh winhttp show proxy` | Docker Desktop → Settings → Resources → **Proxies** 明確關掉 |
| CPU 被佔滿 | `Get-CimInstance Win32_Processor \| Select LoadPercentage` | 找出吃資源的程序 |
| 並行下載互搶 | — | `max-concurrent-downloads: 1` |

**加 Defender 排除（系統管理員）：**

```powershell
Add-MpPreference -ExclusionPath "<Docker 資料磁碟所在資料夾>"
Add-MpPreference -ExclusionProcess "com.docker.backend.exe"
Add-MpPreference -ExclusionProcess "com.docker.build.exe"
Add-MpPreference -ExclusionProcess "vmmem.exe"
Add-MpPreference -ExclusionProcess "vmmemWSL.exe"
Add-MpPreference -ExclusionProcess "wslservice.exe"

(Get-MpPreference).ExclusionPath
```

> **搬過磁碟位置的話一定要重加。** Docker Desktop 安裝時加的排除指向原本的
> `%LOCALAPPDATA%\Docker`，搬到新磁碟之後那條規則就失效了。
>
> 順帶一提：檢查排除清單時如果看到 `C:\Windows\system32` 或 `powershell.EXE`
> 被排除，那**不是正常設定**，是惡意或流氓軟體常用的手法，建議移除並做一次掃描。

### 14.15 全部砍掉重來

```powershell
docker compose down -v
docker compose up -d --build
```

`-v` 會刪掉**所有 volume**：資料庫、模型、Open WebUI 帳號全部消失，
模型要重新下載 11 GB。執行前請先備份資料庫（[12.5](#125-備份與還原)）
與模型（[12.8](#128-備份與還原-ollama-模型)）。

---

## 15. 專案目錄結構

```text
ipqc-spc-system/
├─ docker-compose.yml          ← 整套系統的定義，9 個服務都在這裡
├─ init-realdb.sh              ← 資料庫初始化腳本（還原 dump + 套用 migrations）
├─ REALDB_backup.dump          ← PostgreSQL 18.3 custom format dump（schema + 初始資料）
├─ .env.example                ← 環境變數範例，複製成 .env 再填
├─ .env                        ← ⚠️ 你要自己建立，已被 .gitignore 排除
├─ .gitattributes              ← 強制 .sh 為 LF，避免 Windows CRLF 讓容器起不來
├─ README.md                   ← 本文件：從零開始的完整部署
├─ DEPLOY.md                   ← 部署與維運：CI 檢查、版本發布、穩定性設定
├─ OFFLINE-INSTALL.md          ← 離線安裝包：製作、上傳 Release、目標機器安裝
│
├─ .github/workflows/
│  └─ ci.yml                   ← 快速檢查：typecheck / 語法 / compose / CRLF
│
├─ scripts/
│  ├─ pin-upstream-images.sh   ← 把上游 image 鎖定成 digest 寫回 .env
│  ├─ backup-db.sh             ← REALDB 備份 + 自動輪替
│  ├─ ollama-models.ps1        ← 匯出／還原 11GB 模型（Windows）
│  ├─ ollama-models.sh         ← 同上（macOS / Linux）
│  ├─ make-offline-bundle.ps1  ← 產生離線安裝包（打包 + 壓縮 + 切割 + 校驗碼）
│  └─ install-offline.ps1      ← 目標機器上從離線包安裝
│
├─ dashboard/                  ← SPC 管制圖前端（Next.js 14 + TypeScript）
│  ├─ app/
│  │  ├─ page.tsx              主頁面（五區塊單畫面版面）
│  │  ├─ layout.tsx, globals.css
│  │  └─ api/                  17 個 API route（全部標了 force-dynamic）
│  │     ├─ chart/             管制圖資料
│  │     ├─ capability/        製程能力（Cp/Cpk/Cpm/Cpmk/Ppk）
│  │     ├─ control-limit/     trial / approve / review 的 Phase I 流程
│  │     ├─ measurements/      ingest / notify（外部寫入量測值）
│  │     ├─ event-intervals/, event-types/
│  │     ├─ ai-summary/        代理到 spc-api 的 AI 摘要
│  │     └─ config/, db-info/, schema-check/, neon-diagnose/, processes/
│  ├─ components/              ControlChart / MovingRangeChart / DailyTrendChart
│  ├─ lib/                     db.ts, neon.ts, spcClient.ts, subgroups.ts, config.ts …
│  ├─ db/migrations/           冪等的 schema 變更腳本
│  ├─ Dockerfile               三階段建置，最終 image ≈ 150 MB
│  ├─ README.md                前端說明
│  └─ HANDOFF.md               ⭐ 交接文件：schema 細節、SPC 規則、踩過的坑
│
├─ pipelines/                  ← 檢查表辨識服務（FastAPI + LangGraph + PyMuPDF）
│  ├─ aniki_api.py             FastAPI 入口（容器 CMD 跑的就是這個）
│  ├─ Aniki.py                 辨識主流程
│  ├─ metadatareader.py        讀表頭（品號、製程、機台、日期…）
│  ├─ rowreader.py             讀固定格式的資料列
│  ├─ generic_rowreader.py     讀一般表格
│  ├─ generic_fix.py           修正辨識結果
│  ├─ neon_db.py               寫入 PostgreSQL（名稱沿用舊版，現在連本機 DB）
│  ├─ app.py                   人工確認網頁（掛在同一個 FastAPI，不另開 port）
│  ├─ openwebui_aniki_pipe.py  Open WebUI ↔ FastAPI 的橋接
│  ├─ openwebui_bootstrap.py   自動安裝／啟用 Aniki 並設為預設模型
│  ├─ check_pdf_direction.py   除錯工具：檢查 PDF 頁面方向
│  ├─ Dockerfile, requirements.txt
│  └─ README.md
│
├─ spc_model/                  ← Python SPC 計算服務（FastAPI，容器內部才看得到）
│  ├─ app/
│  │  ├─ main.py               6 個端點（/health + 5 個 /spc/*）
│  │  ├─ spc.py                管制界線、Western Electric / Nelson 規則、Cp/Cpk
│  │  ├─ models.py             Pydantic 請求／回應模型
│  │  ├─ ai_summary.py         AI 摘要的提示詞組裝
│  │  └─ llm.py                呼叫 Ollama
│  ├─ Dockerfile, requirements.txt
│  └─ README.md
│
└─ ipqc/                       ← 檢查表原始檔（bind mount 到 pipelines 的 /app/ipqc）
   └─ *.pdf                    20 多份真實 IPQC 檢查表，可直接拿來測試
```

---

## 16. 安全性注意事項

這套系統的預設設定是**給實驗室／區域網路內的專題展示用**，直接對外開放前請先處理：

| 項目 | 現況 | 上線前建議 |
| --- | --- | --- |
| `.env` | 明碼存密碼與 API Key | 確認不會被提交；權限設為僅本人可讀 |
| `ANIKI_API_KEY` | 唯一的 API 驗證機制 | 用 32 字元以上隨機字串，定期更換 |
| `SETTINGS_PASSWORD` | 不設就是 `spc1234` | **一定要改掉** |
| PostgreSQL | `5431` 對主機開放，`postgres` 超級使用者 | 移除 `ports` 對外映射，或改用權限較小的專用帳號 |
| Ollama | `11434` 無驗證對主機開放 | 不需要外部存取的話移除 `ports` 映射 |
| Pipelines `/docs` | Swagger UI 公開 | 正式環境可關閉或加上反向代理驗證 |
| 人工確認頁 `/review/<token>` | 只靠 token 保護，無登入 | 放在內網，或加上反向代理驗證 |
| 全部服務 | HTTP，沒有 TLS | 對外時前面放 Nginx / Caddy 做 HTTPS |

**辨識資料不會離開這台電腦**——Ollama 在本機推論，資料庫在本機容器，
整套系統除了第一次下載映像與模型之外，不需要連外。

---

## 附錄 A：macOS / Linux 的差異

核心流程（建 `.env` → `docker compose up -d --build` → 驗證）完全一樣，
只有安裝 Docker 與指令語法不同。

### macOS（Apple Silicon / Intel）

1. 下載並安裝 **Docker Desktop for Mac**（注意選對晶片版本）。
2. Docker Desktop → **Settings → Resources**：Memory 調到 12 GB 以上，Disk 40 GB 以上。
3. Apple Silicon 上 Ollama 目前**無法在 Docker 容器內使用 Metal GPU 加速**，
   容器內是純 CPU 推論。想用 GPU 的話，要把 Ollama 裝在 macOS 主機上
   （`brew install ollama`），然後把 compose 裡各服務的
   `http://ollama:11434` 改成 `http://host.docker.internal:11434`，
   並移除 `ollama` 與 `ollama-model-init` 兩個服務。
4. 指令改用 Terminal：

   ```bash
   git clone https://github.com/<你的帳號>/ipqc-spc-system.git
   cd ipqc-spc-system
   nano .env            # 或 code .env
   docker compose up -d --build
   ```

5. 產生隨機 API Key：

   ```bash
   openssl rand -hex 24
   ```

6. 查埠佔用：

   ```bash
   lsof -i :3000 -i :3001 -i :8000 -i :11434 -i :5431
   ```

### Linux（Ubuntu 22.04 / 24.04）

1. 安裝 Docker Engine + Compose plugin：

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER
   newgrp docker
   docker compose version
   ```

2. （選用）NVIDIA GPU：安裝 NVIDIA 驅動與 NVIDIA Container Toolkit，

   ```bash
   sudo apt install -y nvidia-container-toolkit
   sudo nvidia-ctk runtime configure --runtime=docker
   sudo systemctl restart docker
   docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
   ```

   然後照 [5.3](#53選用啟用-nvidia-gpu-加速) 在 `ollama` 服務加上 `deploy.resources`。
   Linux 上這個做法可以直接用到 GPU，效果最好。

3. 其餘步驟相同：

   ```bash
   git clone https://github.com/<你的帳號>/ipqc-spc-system.git
   cd ipqc-spc-system
   nano .env
   docker compose up -d --build
   ```

4. 查埠佔用：

   ```bash
   sudo ss -tulpn | grep -E ':(3000|3001|8000|11434|5431)'
   ```

5. Linux 沒有 CRLF 問題（14.7 用不到）。

---

## 附錄 B：API 端點索引

### Pipelines（<http://localhost:8000>）

| 方法 | 路徑 | 驗證 | 說明 |
| --- | --- | --- | --- |
| `GET` | `/health` | 否 | 健康檢查，回報 API Key 是否設定、輸入資料夾是否存在 |
| `GET` | `/docs` | 否 | Swagger UI |
| `GET` | `/file-status?file_name=` | `x-api-key` | 查詢資料庫是否已有同名檔案 |
| `POST` | `/process` | `x-api-key` | 上傳並辨識檔案（單一或多個），寫入 PostgreSQL |
| `GET` | `/review/{token}` | token | 人工確認網頁 |
| `POST` | `/api/reviews` | 否 | 建立人工確認任務 |
| `GET` | `/api/reviews/{token}` | token | 取得確認資料（JSON） |
| `POST` | `/api/reviews/{token}/confirm` | token | 送出確認結果 |
| `POST` | `/api/reviews/{token}/edit` | token | 重新開啟已確認的任務 |
| `GET` | `/review-demo/create` | 否 | 產生示範用的確認任務 |

### Dashboard（<http://localhost:3001>）

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| `GET` | `/api/processes` | 品號／製程／機台清單 |
| `GET` | `/api/event-types?machine=` | 該機台實際存在的事件類型 |
| `GET` | `/api/event-intervals` | 事件區間清單（含樣本數與 `管制開始時間`） |
| `GET` | `/api/chart` | 管制圖資料（點、界線、異常旗標） |
| `GET` | `/api/capability` | 製程能力表 + 選定尺寸明細 + 每日趨勢 |
| `GET` | `/api/control-limit` | 目前生效的管制界線 |
| `POST` | `/api/control-limit/trial` | Phase I 試算（**不寫 DB**） |
| `POST` | `/api/control-limit/review` | 記錄疑似異常點的人工覆核結果 |
| `POST` | `/api/control-limit/approve` | 核准並進入 Phase II（伺服器重算後寫入） |
| `POST` | `/api/rebuild-all-control-limits` | 舊版批次重建（保留作維護用，UI 不呼叫） |
| `POST` | `/api/measurements/ingest` | 外部寫入工件與量測值 |
| `POST` | `/api/measurements/notify` | 量測值分析通知 |
| `POST` | `/api/ai-summary` | 代理到 `spc-api` 產生中文摘要 |
| `GET` / `POST` | `/api/config` | 讀取／更新 `min_samples`、`cpk_threshold`、`auto_create_control_limit` |
| `GET` | `/api/db-info` | 資料庫連線資訊 |
| `GET` | `/api/schema-check` | 檢查必要的表與 view |
| `GET` | `/api/neon-diagnose` | 連線診斷 |

### SPC API（僅容器內部 `http://spc-api:8000`）

| 方法 | 路徑 | 說明 |
| --- | --- | --- |
| `GET` | `/health` | 健康檢查 |
| `POST` | `/spc/build-chart-data` | 產生管制圖資料點與界線（含規則判定） |
| `POST` | `/spc/calculate-trial-limits` | 計算 Phase I 試算界線 |
| `POST` | `/spc/capability` | 計算 Cp / Cpk / Cpm / Cpmk / Ppk |
| `POST` | `/spc/analyze-measurement` | 單點判定（超規／失控） |
| `POST` | `/spc/ai-summary` | 呼叫 Ollama 產生中文摘要 |

---

## 延伸閱讀

| 文件 | 內容 |
| --- | --- |
| [`OFFLINE-INSTALL.md`](OFFLINE-INSTALL.md) | ⭐ 離線安裝包：沒有外網的機器怎麼裝，含 GitHub 檔案大小限制與分割上傳 |
| [`DEPLOY.md`](DEPLOY.md) | ⭐ 部署與維運：CI 檢查、版本發布、log 輪替、image 鎖版、資料庫與模型備份 |
| [`dashboard/HANDOFF.md`](dashboard/HANDOFF.md) | ⭐ 最重要的技術文件：schema 語意、SPC 規則範圍、Phase I/II 流程、各種「看起來對其實錯」的陷阱 |
| [`dashboard/README.md`](dashboard/README.md) | 前端架構與功能說明 |
| [`dashboard/DOCKER.md`](dashboard/DOCKER.md) | Dashboard 單獨建置與執行 |
| [`pipelines/README.md`](pipelines/README.md) | 辨識服務的架構、功能與檔案職責 |
| [`spc_model/README.md`](spc_model/README.md) | SPC 服務單獨建置與執行 |
