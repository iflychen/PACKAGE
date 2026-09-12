# 離線安裝包

給**沒有外網、或網路不穩**的機器用。整套系統從零安裝完成，全程不需要連外。

> 一般有網路的環境請看 [README.md](README.md)（從原始碼建置）或
> [DEPLOY.md](DEPLOY.md)（從 GHCR 拉預先建好的 image）。

---

## 為什麼需要這個

| 問題 | 離線包怎麼解決 |
| --- | --- |
| 工廠機台沒有外網 | 所有 image 與模型都在包裡 |
| `ghcr.io` 從某些線路下載極慢（[README 14.11](README.md#1411-ghcrio-下載極慢或卡在-0-bytes)） | 不經過 registry |
| 每台機器裝出來的結果可能不同 | 同一份 image，位元相同 |
| 11 GB 模型要重下 | 模型跟著包走 |

現場安裝時間從「20–60 分鐘且可能失敗」變成「**5–10 分鐘、零網路、結果固定**」。

---

## GitHub 的檔案大小限制

這是設計成分割檔的原因：

| 限制 | 數值 |
| --- | --- |
| repo 內單一檔案 | **100 MB**（超過 push 直接被拒） |
| Release 附件單檔 | **2 GB** |

所以幾 GB 的打包檔**不能放進 repo**，只能當 Release 附件，而且要切成 ≤2 GB 的分割檔。
`make-offline-bundle.ps1` 預設切成 1900 MB 一份。

---

## 一、製作（在網路順的機器上）

先把系統完整跑起來一次，確認沒問題，再打包：

```powershell
cd <專案資料夾>

# 只含 Docker image（推薦）
.\scripts\make-offline-bundle.ps1 -OutDir E:\bundle

# 連 11 GB 的 Ollama 模型一起包（目標機器完全沒有外網時）
.\scripts\make-offline-bundle.ps1 -OutDir E:\bundle -IncludeModels
```

> **`-OutDir` 不要指到 repo 資料夾裡。** 腳本會擋，但別試。
> `.gitignore` 也已經排除 `*.tar` / `*.tar.gz`。

產出：

```text
E:\bundle\
├─ images.tar.gz.part00 ~ partNN      所有 Docker image（gzip 後切割）
├─ ollama_models.tar.part00 ~ ...     只有 -IncludeModels 才有
├─ SHA256SUMS.txt                     每個分割檔的校驗碼
├─ manifest.json                      版本、image 清單、切割資訊
├─ install-offline.ps1                安裝端腳本
└─ OFFLINE-INSTALL.md                 這份文件
```

**大小參考**（實際值依版本而異）：

| 內容 | 未壓縮 | gzip 後 | 分割數 |
| --- | --- | --- | --- |
| Docker image | 約 8–10 GB | 約 3–5 GB | 2–3 |
| Ollama 模型 | 約 11 GB | 幾乎不會變小（gguf 本來就壓過） | 6 |

模型不壓縮是刻意的——壓不動，只是白花時間。

---

## 二、上傳到 GitHub Release

```powershell
cd <專案資料夾>

gh release create offline-v1.0.0 (Get-ChildItem E:\bundle\* -File) `
  --title "離線安裝包 v1.0.0" `
  --notes-file OFFLINE-INSTALL.md
```

沒裝 [GitHub CLI](https://cli.github.com/) 的話：repo → **Releases → Draft a new release**
→ 建一個 tag → 把 `E:\bundle` 裡的檔案**全部**拖進附件區 → Publish。

> Release 附件不佔 repo 容量，也不計入 Git LFS 配額。
> Private repo 的 Release 一樣是 private，下載需要登入。

---

## 三、在目標機器安裝

### 3.1 前置

1. 安裝 **Docker Desktop**（或 Linux 的 Docker Engine）並啟動，等狀態轉綠
2. 取得專案原始碼——離線包裡只有 image 和模型，**沒有**原始碼。
   compose 還需要 `REALDB_backup.dump`、`init-realdb.sh`、`dashboard/db/migrations/`、
   `pipelines/openwebui_*.py`、`ipqc/` 這些檔案。

   有網路的話 `git clone`；完全沒網路就把整個資料夾拷過去（含 `.git` 也可以）。

3. 下載 Release 裡的**所有**檔案到同一個資料夾

### 3.2 執行

```powershell
cd <專案資料夾>
E:\bundle\install-offline.ps1
```

或指定路徑：

```powershell
E:\bundle\install-offline.ps1 -BundleDir E:\bundle -RepoDir C:\projects\PACKAGE
```

腳本會依序：驗證校驗碼 → 合併分割檔 → `docker load` → 還原模型 →
提示填 `.env` → `docker compose up -d`。

### 3.3 驗證

```powershell
docker compose ps
docker compose exec ollama ollama list
docker compose exec postgres psql -U postgres -d REALDB -c "\dt"
```

- <http://localhost:3000> — Open WebUI（**第一個註冊的帳號是管理員**）
- <http://localhost:3001> — SPC Dashboard
- <http://localhost:8000/health>

---

## 疑難排解

### 校驗碼不符

那個分割檔下載壞了，重新下載它就好，不用整包重來。

### `docker load` 失敗，說檔案損毀

分割檔沒有全部到齊，或合併順序錯了。確認 `part00`、`part01`… 連號沒有缺，
然後刪掉合併出來的 `images.tar.gz` 重跑一次。

### 空間不足

合併的過程中會同時存在「分割檔」和「合併後的大檔」，**峰值需要約兩倍空間**。
先確認目標磁碟夠：

```powershell
Get-Volume -DriveLetter E | Select-Object DriveLetter,
  @{n='FreeGB';e={[math]::Round($_.SizeRemaining/1GB,1)}}
```

`install-offline.ps1` 在載入成功後會自動刪掉合併檔，只留分割檔。

### 想省事，不要模型

不加 `-IncludeModels` 打包，安裝端啟動時由 `ollama-model-init` 自行下載 11 GB。
目標機器有外網、只是 ghcr 慢的話，這樣包小很多（3–5 GB vs 15 GB），
而 ollama 官方 CDN 通常夠快。

---

## 更新離線包

程式碼改過之後重做一份即可：

```powershell
git pull
docker compose build
.\scripts\make-offline-bundle.ps1 -OutDir E:\bundle2
gh release create offline-v1.1.0 (Get-ChildItem E:\bundle2\* -File) --title "離線安裝包 v1.1.0"
```

目標機器只要重跑 `install-offline.ps1`。`docker load` 會覆蓋同名 image，
資料庫與模型的 volume 不受影響。
