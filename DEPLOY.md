# 部署與維運指南

> 第一次安裝、從零架環境請看 [README.md](README.md)。
> 完全沒有外網的機器請看 [OFFLINE-INSTALL.md](OFFLINE-INSTALL.md)。
> 這份文件講的是**怎麼讓這套系統長期穩定地跑下去**，以及發布新版本的流程。

---

## 一、兩種部署方式

這個 repo 目前**不使用 container registry**，所以 image 有兩個來源：

| 方式 | 適用 | 現場要做什麼 | 時間 |
| --- | --- | --- | --- |
| **從原始碼建置** | 開發、改程式碼、有網路的環境 | `docker compose up -d --build` | 首次 20–60 分鐘 |
| **離線安裝包** | 網路慢、卡在 ghcr.io、或完全沒外網 | 下載 Release 附件 → `install-offline.ps1` | 5–10 分鐘 |

離線包的製作與使用見 [OFFLINE-INSTALL.md](OFFLINE-INSTALL.md)，
下載安裝的部分也寫在 [README 8.1](README.md#81選用網路太慢的話從-release-下載離線包)。

### 1.1 CI 會幫你檢查什麼

`.github/workflows/ci.yml` 在每次 push 與 PR 時跑，只用預設的唯讀權限，
不需要任何額外設定：

| 檢查 | 擋掉什麼 |
| --- | --- |
| Dashboard typecheck | TypeScript 型別錯誤 |
| Python 語法（`compileall`） | 打錯字、縮排錯誤 |
| `docker compose config` | compose 語法錯誤、變數寫錯 |
| `shellcheck` | shell script 的真正錯誤（warning 只提示不擋） |
| **CRLF 偵測** | `.sh` 被轉成 CRLF —— 這會讓 `database-init` 無限重啟（[README 14.7](README.md#147-database-init-一直-restarting或報-no-such-file-or-directory)） |

刻意保持快（不裝 Python 相依套件、不跑 `next build`），目的是三十秒內告訴你有沒有打錯字。

### 1.2 發布新版本

沒有 registry，所以「發布」＝**做一份新的離線包並建立 Release**：

```powershell
git pull
docker compose build                                   # 重建三個自製 image
.\scripts\make-offline-bundle.ps1 -OutDir E:\bundle    # 打包

git tag v1.1.0
git push origin v1.1.0

gh release create v1.1.0 (Get-ChildItem E:\bundle\* -File) `
  --title "v1.1.0" --notes-file OFFLINE-INSTALL.md
```

目標機器重跑 `install-offline.ps1` 即可。`docker load` 會覆蓋同名 image，
資料庫與模型的 volume 不受影響。

> **升級前先備份。** `.\scripts\backup-db.sh` 與 `.\scripts\ollama-models.ps1 -Action export`。
> migration 只有前進的方向，回退不會自動回退 schema。

---

## 二、穩定性設定

以下是這次一併加入的東西，都是長期運轉才會遇到的問題。

### 2.1 Log 輪替

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

### 2.2 鎖定上游 image 版本

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

### 2.3 資料庫備份

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

### 2.4 Ollama 模型備份

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

這也是[離線安裝包](OFFLINE-INSTALL.md)的一塊拼圖——模型能離線帶走，
剩下的就是把 image 一起 `docker save` 打包。

### 2.5 `.gitattributes` 強制 LF

Windows 版 Git 預設 `core.autocrlf=true`，會把 `init-realdb.sh` 轉成 CRLF，
容器內的 shell 就會去找 `/bin/sh\r` 這個不存在的直譯器，`database-init` 直接掛掉。

`.gitattributes` 把所有在容器內執行的檔案鎖成 LF，不管使用者的 Git 怎麼設定。
`ci.yml` 另外有一個步驟會主動偵測 CRLF 並讓 CI 失敗。

### 2.6 Dashboard 健康檢查

`dashboard` 原本沒有 healthcheck，`docker compose ps` 只會顯示 `Up`，
看不出它到底有沒有真的在服務。現在會顯示 `Up (healthy)`。

用 `node -e` 而不是 `curl` / `wget`：最終 image 是 `node:20-alpine`，
node 一定在，curl 不一定在。

### 2.7 修掉模型設定不一致

舊版 `docker-compose.yml` 裡，`ollama-model-init` 用 `${SPC_OLLAMA_MODEL}` 決定**下載**哪個模型，
但 `spc-api` 的 `OLLAMA_MODEL` 是**寫死 `qwen2.5:7b`**。
改 `.env` 的 `SPC_OLLAMA_MODEL` 只會下載新模型，AI 摘要仍然去呼叫舊的，然後失敗。

現在兩邊都讀同一個變數。

### 2.8 移除誤入版控的 `.pyc`

`spc_model/app/__pycache__/` 下有六個編譯產物被加進版控。
已經移除並加入 `.gitignore`。

---

## 三、環境變數速查（新增的部分）

| 變數 | 用在哪 | 預設 | 說明 |
| --- | --- | --- | --- |
| `POSTGRES_IMAGE` | compose | `postgres:18` | 可用 `pin-upstream-images.sh` 鎖成 digest |
| `OLLAMA_IMAGE` | compose | `ollama/ollama:latest` | 同上 |
| `OPENWEBUI_IMAGE` | compose | `ghcr.io/open-webui/open-webui:main` | 同上，**最建議鎖定的一個** |
| `PYTHON_INIT_IMAGE` | compose | `python:3.12-slim` | 同上 |
| `PGPOOL_MAX` | compose | `10` | Dashboard 的 PostgreSQL 連線池上限 |
| `BACKUP_KEEP` | 備份腳本 | `14` | 保留幾份資料庫備份 |
| `OLLAMA_VOLUME` | 模型腳本 | `real-project_ollama_data` | 要匯出／還原哪個 volume |

完整清單見 [`.env.example`](.env.example)。

---

## 四、之後可以做的事

### 4.1 改用 container registry（需要 repo 管理員權限）

目前沒有走這條路，是因為推 image 到 GitHub Container Registry 需要
**Settings → Actions → General → Workflow permissions** 設成
**Read and write permissions**，那是 repo **管理員**才看得到的選項。

之後如果管理員願意開，好處是現場只要 `docker compose pull`，
完全不用 build 也不用搬 USB。要做的事：

1. 管理員把 Workflow permissions 改成 Read and write
2. 新增一個 workflow，用 `docker/build-push-action` 把
   `dashboard` / `pipelines` / `spc_model` 三個 image 推到
   `ghcr.io/<owner>/<name>`，認證用 Actions 自動提供的 `GITHUB_TOKEN`
3. 新增一份只有 `image:` 沒有 `build:` 的 compose，
   image 路徑用 `ghcr.io/${GHCR_OWNER}/...:${IMAGE_TAG}` 這種變數形式
4. 部署端 `docker compose -f <那份> pull && up -d`

在那之前，離線安裝包提供的是同一件事的效果：**現場不必編譯**。

### 4.2 搬到 Linux 主機

避開 Docker Desktop 的商用授權（員工 250 人或年營收 1000 萬美元以上需付費），
GPU 直通也更單純，長期運轉比 Windows + WSL2 穩定。

### 4.3 廠內 registry mirror

要上多台機器的話，與其一台一台搬 USB，不如在內網架一台 registry
（`registry:2`、Harbor 或 Zot），用離線包灌一次，之後所有機器從內網拉。
