<#
.SYNOPSIS
    從離線安裝包還原整套系統。在**目標機器**上執行，不需要外網。

.DESCRIPTION
    這個腳本會：
      1. 驗證所有分割檔的 SHA256
      2. 合併分割檔
      3. docker load 載入所有 image
      4. 還原 Ollama 模型（如果包裡有）
      5. 提示建立 .env
      6. docker compose up -d

    前置需求：目標機器已安裝 Docker Desktop（或 Docker Engine）並正在執行，
    而且已經取得專案原始碼（git clone 或複製資料夾）—— 因為 compose 需要
    REALDB_backup.dump、init-realdb.sh、migrations、ipqc 這些檔案。

.PARAMETER BundleDir
    放分割檔的資料夾。預設是這個腳本所在的位置。

.PARAMETER RepoDir
    專案原始碼位置。預設是目前所在目錄。

.EXAMPLE
    .\install-offline.ps1
    .\install-offline.ps1 -BundleDir E:\bundle -RepoDir C:\projects\PACKAGE
#>

[CmdletBinding()]
param(
    [string]$BundleDir = $PSScriptRoot,
    [string]$RepoDir   = (Get-Location).Path,
    [string]$Volume    = 'real-project_ollama_data',
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'

function Join-Parts {
    param([string]$Dir, [string]$BaseName)

    $parts = Get-ChildItem $Dir -Filter "$BaseName.part*" | Sort-Object Name
    if (-not $parts) {
        # 沒切割過，本來就是完整檔
        $whole = Join-Path $Dir $BaseName
        if (Test-Path $whole) { return $whole }
        return $null
    }

    $target = Join-Path $Dir $BaseName
    Write-Host "合併 $($parts.Count) 個分割檔 -> $BaseName ..." -ForegroundColor Cyan

    $out = [System.IO.File]::Create($target)
    try {
        foreach ($p in $parts) {
            Write-Host "  $($p.Name)"
            $in = [System.IO.File]::OpenRead($p.FullName)
            try { $in.CopyTo($out, 1MB) } finally { $in.Dispose() }
        }
    } finally { $out.Dispose() }

    return $target
}

# ---------------------------------------------------------------------------
Write-Host "=== 離線安裝 ===" -ForegroundColor Green

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "找不到 docker 指令。請先安裝並啟動 Docker Desktop。"
}
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker 沒有在執行，請先啟動 Docker Desktop 並等待狀態轉綠。" }

if (-not (Test-Path (Join-Path $RepoDir 'docker-compose.yml'))) {
    throw "在 $RepoDir 找不到 docker-compose.yml。用 -RepoDir 指定專案原始碼的位置。"
}

# --- 1. 驗證校驗碼 ---------------------------------------------------------
$sumFile = Join-Path $BundleDir 'SHA256SUMS.txt'
if ((Test-Path $sumFile) -and -not $SkipVerify) {
    Write-Host "`n[1/5] 驗證校驗碼..." -ForegroundColor Yellow
    $bad = 0
    foreach ($line in Get-Content $sumFile) {
        if ($line -match '^([0-9a-f]{64})\s+(.+)$') {
            $expect = $Matches[1]
            $name   = $Matches[2].Trim()
            $path   = Join-Path $BundleDir $name
            if (-not (Test-Path $path)) {
                Write-Host "  缺少 $name" -ForegroundColor Red; $bad++; continue
            }
            $actual = (Get-FileHash $path -Algorithm SHA256).Hash.ToLower()
            if ($actual -ne $expect) {
                Write-Host "  校驗碼不符 $name" -ForegroundColor Red; $bad++
            } else {
                Write-Host "  OK $name" -ForegroundColor DarkGray
            }
        }
    }
    if ($bad -gt 0) {
        throw "$bad 個檔案有問題。重新下載那幾個分割檔再試一次。"
    }
} else {
    Write-Host "`n[1/5] 略過校驗" -ForegroundColor DarkGray
}

# --- 2. 合併並載入 image ---------------------------------------------------
Write-Host "`n[2/5] 合併並載入 image..." -ForegroundColor Yellow
$imagesGz = Join-Parts -Dir $BundleDir -BaseName 'images.tar.gz'
if (-not $imagesGz) { throw "找不到 images.tar.gz（或它的分割檔）。" }

Write-Host "docker load（幾 GB，需要數分鐘）..." -ForegroundColor Cyan
# docker load 認得 gzip，不用先解壓縮
docker load -i $imagesGz
if ($LASTEXITCODE -ne 0) { throw "docker load 失敗。" }

# 合併出來的大檔佔空間，載入成功就刪掉（分割檔保留）
if (Get-ChildItem $BundleDir -Filter 'images.tar.gz.part*') {
    Remove-Item $imagesGz -Force
}

# --- 3. 還原模型（如果有）--------------------------------------------------
Write-Host "`n[3/5] Ollama 模型..." -ForegroundColor Yellow
$modelsTar = Join-Parts -Dir $BundleDir -BaseName 'ollama_models.tar'
if ($modelsTar) {
    docker volume create $Volume | Out-Null
    $dir  = Split-Path -Parent $modelsTar
    $file = Split-Path -Leaf   $modelsTar
    docker run --rm -v "${Volume}:/data" -v "${dir}:/backup" `
        alpine tar xf "/backup/$file" -C /data
    if ($LASTEXITCODE -ne 0) { throw "模型還原失敗。" }

    if (Get-ChildItem $BundleDir -Filter 'ollama_models.tar.part*') {
        Remove-Item $modelsTar -Force
    }
    Write-Host "  模型已還原，啟動時會跳過下載。" -ForegroundColor Green
} else {
    Write-Host "  包裡沒有模型，啟動後會下載約 11 GB（需要外網）。" -ForegroundColor DarkGray
}

# --- 4. .env ---------------------------------------------------------------
Write-Host "`n[4/5] 環境變數..." -ForegroundColor Yellow
$envPath = Join-Path $RepoDir '.env'
if (-not (Test-Path $envPath)) {
    $example = Join-Path $RepoDir '.env.example'
    if (Test-Path $example) {
        Copy-Item $example $envPath
        Write-Host "  已從 .env.example 建立 .env" -ForegroundColor Cyan
    }
    Write-Host "  請先填好 DB_PASSWORD 與 ANIKI_API_KEY 再繼續：" -ForegroundColor Yellow
    Write-Host "      notepad `"$envPath`"" -ForegroundColor Yellow
    Read-Host "填好後按 Enter 繼續"
} else {
    Write-Host "  .env 已存在" -ForegroundColor DarkGray
}

# --- 5. 啟動 ---------------------------------------------------------------
Write-Host "`n[5/5] 啟動服務..." -ForegroundColor Yellow
Push-Location $RepoDir
try {
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw "docker compose up 失敗。" }
    Write-Host "`n=== 完成 ===" -ForegroundColor Green
    docker compose ps
} finally { Pop-Location }

Write-Host @"

驗證：
    docker compose exec ollama ollama list
    docker compose exec postgres psql -U postgres -d REALDB -c "\dt"

開啟：
    http://localhost:3000   Open WebUI（第一個註冊的帳號是管理員）
    http://localhost:3001   SPC Dashboard
    http://localhost:8000/health
"@ -ForegroundColor Cyan
