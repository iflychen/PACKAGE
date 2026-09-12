<#
.SYNOPSIS
    匯出／還原 Ollama 模型，省下重新下載 11 GB 的時間。

.DESCRIPTION
    模型存在 Docker 具名 volume real-project_ollama_data 裡（容器內路徑 /root/.ollama），
    不是你在檔案總管打得開的資料夾 —— 它住在 WSL2 的 vhdx 內部。
    要把內容拿出來，標準做法是掛一個一次性容器進去打包。

    什麼時候用得上：
      - 重灌 / 重建 Docker（docker compose down -v 會把模型一起刪掉）
      - 搬到另一台機器、或交付到廠內那台
      - 網路不穩，不想再賭一次 11 GB 的下載

.PARAMETER Action
    export = 匯出成 tar；import = 從 tar 還原

.PARAMETER Path
    tar 檔要放哪 / 從哪讀。預設 E:\docker-backup\ollama_models.tar
    建議放在「不是 C 槽、也不在 git repo 裡面」的位置。

.EXAMPLE
    .\scripts\ollama-models.ps1 -Action export
    .\scripts\ollama-models.ps1 -Action import
    .\scripts\ollama-models.ps1 -Action export -Path D:\backup\models.tar
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('export', 'import')]
    [string]$Action,

    [string]$Path = 'E:\docker-backup\ollama_models.tar',

    # compose 專案名是 real-project（docker-compose.yml 第一行），所以 volume 前綴固定。
    [string]$Volume = 'real-project_ollama_data'
)

$ErrorActionPreference = 'Stop'

function Assert-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "找不到 docker 指令，請先啟動 Docker Desktop。"
    }
    docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Docker 沒有在執行，請先啟動 Docker Desktop 並等待狀態轉綠。"
    }
}

Assert-Docker

$dir  = Split-Path -Parent $Path
$file = Split-Path -Leaf   $Path

if ($Action -eq 'export') {

    $exists = docker volume ls --quiet --filter "name=^$Volume$"
    if (-not $exists) {
        throw "找不到 volume $Volume。系統跑起來過嗎？用 docker volume ls 確認名稱。"
    }

    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }

    Write-Host "匯出 $Volume -> $Path" -ForegroundColor Cyan
    Write-Host "11 GB 大約需要 3-5 分鐘，過程中沒有進度顯示。" -ForegroundColor DarkGray

    # 刻意不壓縮：gguf 權重本來就是壓過的，再壓一次只是浪費時間。
    docker run --rm -v "${Volume}:/data" -v "${dir}:/backup" `
        alpine tar cf "/backup/$file" -C /data .

    if ($LASTEXITCODE -ne 0) { throw "匯出失敗。" }

    $item = Get-Item $Path
    if ($item.Length -lt 1GB) {
        Write-Warning "檔案只有 $([math]::Round($item.Length/1MB)) MB，比預期小很多，請確認模型真的下載完成了。"
    }
    Write-Host ("完成：{0} ({1:N2} GB)" -f $Path, ($item.Length / 1GB)) -ForegroundColor Green

} else {

    if (-not (Test-Path $Path)) {
        throw "找不到 $Path"
    }

    Write-Host "還原 $Path -> $Volume" -ForegroundColor Cyan

    docker volume create $Volume | Out-Null

    docker run --rm -v "${Volume}:/data" -v "${dir}:/backup" `
        alpine tar xf "/backup/$file" -C /data

    if ($LASTEXITCODE -ne 0) { throw "還原失敗。" }

    Write-Host "完成。接著 docker compose up -d，ollama-model-init 會發現模型已存在而跳過下載。" -ForegroundColor Green
    Write-Host "驗證：docker compose exec ollama ollama list" -ForegroundColor DarkGray
}
