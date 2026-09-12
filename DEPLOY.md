# 正式部署指南（CI 建置 + GHCR）

> 第一次安裝、從零架環境請看 [README.md](README.md)。
> 這份文件講的是**怎麼把「在使用者電腦上 build」換成「CI build 一次、現場只 pull」**。

---

## 為什麼要這樣改

原本的部署指令是：

```bash
docker compose up -d --build
```

這一行在**目標機器上**跑了 `npm ci` 和 `pip install`。三個問題：

| 問題 | 後果 |
| --- | --- |
| 現場需要 Node / Python 工具鏈與外網 | 失敗點最多，工廠網路一擋就裝不起來 |
| 套件版本會漂移 | 同一份程式碼在兩台機器上可能 build 出不一樣的結果 |
| image 沒有版本 | 出事沒辦法回退，也不知道現在跑的是哪一版程式碼 |

改成 CI 建置之後：

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

現場不再編譯任何東西，每個版本有明確的 tag，回退只要改一個變數。

---

## 兩個 compose 檔的分工

| 檔案 | 用途 | image 從哪來 |
| --- | --- | --- |
| `docker-compose.yml` | 開發、改程式碼、專題展示 | 本機 `build:` 現場編譯 |
| `docker-compose.prod.yml` | 正式部署 | `ghcr.io/<owner>/...` 直接 pull |

兩個檔的服務定義、連接埠、依賴關係完全一致，差別只在三個自製服務的 image 來源。

> **注意：** `docker-compose.prod.yml` 仍然需要 repo 裡的幾個檔案（以 bind mount 掛進容器）：
> `REALDB_backup.dump`、`init-realdb.sh`、`dashboard/db/migrations/`、
> `pipelines/openwebui_*.py`、`ipqc/`。
> 所以部署機器還是要 clone 這個 repo —— 只是不用再 build。

---

## 一、CI 設定（做一次就好）

### 1.1 兩個 workflow

| Workflow | 何時跑 | 做什麼 |
| --- | --- | --- |
| `.github/workflows/ci.yml` | 每次 push / PR | Dashboard typecheck、Python 語法檢查、compose 驗證、shell 檢查（含 CRLF 偵測） |
| `.github/workflows/build-images.yml` | push 到 main、push `v*.*.*` tag、手動 | 建置三個 image 並推到 GHCR |

`ci.yml` 刻意保持快（不裝 Python 相依套件、不跑 `next build`），目的是在浪費時間 build image 之前先擋掉打錯字。

### 1.2 開啟 GHCR 權限

Repo → **Settings → Actions → General → Workflow permissions**，
確認選的是 **Read and write permissions**。

workflow 已經宣告 `packages: write`，但 repo 層級如果設成唯讀，推送會拿到 403。

### 1.3 第一次建置

```bash
git push origin main
```

到 repo 的 **Actions** 分頁看 `build-images` 跑完。成功後在 **Packages** 分頁會看到三個 package：

```
spc-dashboard
aniki-pipelines
spc-model
```

### 1.4 發布一個正式版本

```bash
git tag v1.0.0
git push origin v1.0.0
```

會產生 `1.0.0` / `1.0` / `1` / `latest` 四個標籤，指向同一個 image。
部署時用完整的 `1.0.0`，不要用 `latest` —— 用 `latest` 就等於沒有版本。

### 1.5 Private repo 的注意事項

Private repo 的 package 預設也是 private，部署機器 pull 之前要先登入：

```bash
echo <你的 Personal Access Token> | docker login ghcr.io -u <你的帳號> --password-stdin
```

PAT 需要 `read:packages` 權限（**Settings → Developer settings → Personal access tokens**）。

另外，個人帳號的 private repo 每月有 **2,000 分鐘** 的免費 Actions 額度。
三個 image 一次完整建置大約 5–12 分鐘，第二次之後有 layer cache 會快很多。
額度快用完的話，可以把 `build-images.yml` 的 `on.push.branches` 拿掉，
只留 tag 觸發 —— 這樣只有真的要發版時才建置。

---

## 二、部署端操作

### 2.1 第一次部署

```bash
git clone <這個 repo 的網址> ipqc-spc-system
cd ipqc-spc-system
cp .env.example .env
```

編輯 `.env`，除了 `DB_PASSWORD` 與 `ANIKI_API_KEY`，再補上這兩行
（CI 跑完後 Actions 的 **Summary** 頁面會直接給你）：

```env
GHCR_OWNER=你的github帳號全小寫
IMAGE_TAG=1.0.0
```

然後：

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

驗證方式和 README 的[步驟五](README.md#9-步驟五驗證部署是否成功)完全一樣。

### 2.2 更新到新版本

```bash
# 1. 先備份
./scripts/backup-db.sh

# 2. 拿最新的 compose 與 migration
git pull

# 3. 改 .env 的 IMAGE_TAG
#    IMAGE_TAG=1.1.0

# 4. 套用
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

`database-init` 會自動重跑 `dashboard/db/migrations/*.sql`。
那些腳本是冪等的，已經套用過的不會重複執行。

### 2.3 回退

```bash
# .env 改回 IMAGE_TAG=1.0.0
docker compose -f docker-compose.prod.yml up -d
```

image 還在本機快取，通常幾秒就完成。

> ⚠️ **回退不會自動回退資料庫 schema。** migration 只有前進的方向。
> 如果新版含有破壞性的 schema 變更，回退前要先從備份還原資料庫。

---

## 三、穩定性設定

以下是這次一併加入的東西，都是長期運轉才會遇到的問題。

### 3.1 Log 輪替

兩個 compose 檔都套了：

```yaml
x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"
```

每個容器最多 30 MB。沒有這段的話，`ollama` 與 `pipelines` 的 log 會一直長，
幾個月後把磁碟塞爆 —— 這是長期運轉的機器最常見的死法。

### 3.2 鎖定上游 image 版本

`postgres:18`、`ollama/ollama:latest`、`open-webui:main` 都是**浮動標籤**。
`open-webui` 特別危險：`openwebui_bootstrap.py` 直接操作它的 `webui.db`，
上游改 schema 就會在某次重開機時**無聲壞掉**。

系統驗證正常之後，執行：

```bash
./scripts/pin-upstream-images.sh
```

它會把「你現在正在跑、已經驗證過的那一版」解析成 digest 寫回 `.env`：

```env
POSTGRES_IMAGE=postgres@sha256:...
OLLAMA_IMAGE=ollama/ollama@sha256:...
OPENWEBUI_IMAGE=ghcr.io/open-webui/open-webui@sha256:...
PYTHON_INIT_IMAGE=python@sha256:...
```

digest 是內容雜湊，永遠指向同一個 image。
只想看目前 digest 不改檔案的話加 `--show`。

要升級上游版本時，把該行改回浮動標籤 → `docker compose pull` → 測 → 再跑一次這個腳本。

### 3.3 資料庫備份

```bash
./scripts/backup-db.sh                  # 存到 ./backups/
./scripts/backup-db.sh /mnt/nas/spc     # 存到指定位置
```

預設保留最新 14 份，用 `BACKUP_KEEP=30` 可以調整。

排程（Linux，每天凌晨 3 點）：

```cron
0 3 * * * cd /path/to/ipqc-spc-system && ./scripts/backup-db.sh >> backups/cron.log 2>&1
```

排程（Windows 工作排程器）：

```
wsl -e bash -c "cd /mnt/c/path/to/ipqc-spc-system && ./scripts/backup-db.sh"
```

### 3.4 Ollama 模型備份

模型有 11 GB，重灌或搬機器時重新下載很花時間，網路不穩的環境更是賭博。

```powershell
.\scripts\ollama-models.ps1 -Action export      # 預設 E:\docker-backup\ollama_models.tar
.\scripts\ollama-models.ps1 -Action import
```

```bash
./scripts/ollama-models.sh export
./scripts/ollama-models.sh import
```

還原之後 `docker compose up -d`，`ollama-model-init` 會發現模型已存在而跳過下載。
tar 檔請放在 repo 外面、而且不要放 C 槽（`.gitignore` 已擋掉 `*.tar`，但別靠它）。

這也是[第五節](#五離線安裝包)離線安裝包的第一塊拼圖——模型能離線帶走，
剩下的就是把 image 一起 `docker save` 打包。

### 3.5 `.gitattributes` 強制 LF

Windows 版 Git 預設 `core.autocrlf=true`，會把 `init-realdb.sh` 轉成 CRLF，
容器內的 shell 就會去找 `/bin/sh\r` 這個不存在的直譯器，`database-init` 直接掛掉。

`.gitattributes` 把所有在容器內執行的檔案鎖成 LF，不管使用者的 Git 怎麼設定。
`ci.yml` 另外有一個步驟會主動偵測 CRLF 並讓 CI 失敗。

### 3.6 Dashboard 健康檢查

`dashboard` 原本沒有 healthcheck，`docker compose ps` 只會顯示 `Up`，
看不出它到底有沒有真的在服務。現在會顯示 `Up (healthy)`。

用 `node -e` 而不是 `curl` / `wget`：最終 image 是 `node:20-alpine`，
node 一定在，curl 不一定在。

### 3.7 修掉模型設定不一致

舊版 `docker-compose.yml` 裡，`ollama-model-init` 用 `${SPC_OLLAMA_MODEL}` 決定**下載**哪個模型，
但 `spc-api` 的 `OLLAMA_MODEL` 是**寫死 `qwen2.5:7b`**。
改 `.env` 的 `SPC_OLLAMA_MODEL` 只會下載新模型，AI 摘要仍然去呼叫舊的，然後失敗。

現在兩邊都讀同一個變數。

### 3.8 移除誤入版控的 `.pyc`

`spc_model/app/__pycache__/` 下有六個編譯產物被加進版控。
已經移除並加入 `.gitignore`。

---

## 四、環境變數速查（新增的部分）

| 變數 | 用在哪 | 預設 | 說明 |
| --- | --- | --- | --- |
| `GHCR_OWNER` | prod | 無，必填 | GHCR 擁有者帳號，**全小寫** |
| `IMAGE_TAG` | prod | `latest` | 要部署的版本，建議填完整版號 |
| `POSTGRES_IMAGE` | 兩者 | `postgres:18` | 可用 `pin-upstream-images.sh` 鎖成 digest |
| `OLLAMA_IMAGE` | 兩者 | `ollama/ollama:latest` | 同上 |
| `OPENWEBUI_IMAGE` | 兩者 | `ghcr.io/open-webui/open-webui:main` | 同上，**最建議鎖定的一個** |
| `PYTHON_INIT_IMAGE` | 兩者 | `python:3.12-slim` | 同上 |
| `PGPOOL_MAX` | 兩者 | `10` | Dashboard 的 PostgreSQL 連線池上限 |
| `BACKUP_KEEP` | 備份腳本 | `14` | 保留幾份備份 |
| `OLLAMA_VOLUME` | 模型腳本 | `real-project_ollama_data` | 要匯出／還原哪個 volume |

完整清單見 [`.env.example`](.env.example)。

---

## 五、離線安裝包

已經實作，見 **[OFFLINE-INSTALL.md](OFFLINE-INSTALL.md)**。

```powershell
.\scripts\make-offline-bundle.ps1 -OutDir E:\bundle          # 製作
gh release create offline-v1.0.0 (Get-ChildItem E:\bundle\* -File)   # 上傳
E:\bundle\install-offline.ps1                                # 目標機器安裝
```

重點：GitHub 的 repo **不接受 > 100 MB 的檔案**，Release 附件單檔上限 **2 GB**，
所以幾 GB 的打包檔只能當 Release 附件並切成分割檔。腳本已經處理好切割與校驗碼。

## 六、下一步（尚未實作）

**搬到 Linux 主機** —— 避開 Docker Desktop 的商用授權（員工 250 人或年營收
1000 萬美元以上需付費），GPU 直通也更單純，長期運轉比 Windows + WSL2 穩定。
