<#
.SYNOPSIS
    產生離線安裝包：把所有 Docker image（可選加上 Ollama 模型）打包、壓縮、
    切成可以上傳到 GitHub Release 的分割檔。

.DESCRIPTION
    為什麼需要這個：
      - 工廠的機器常常沒有外網
      - ghcr.io 從某些線路下載極慢（見 README 14.11）
      - 現場安裝不該賭網路

    為什麼要切割：
      GitHub 的 repo 不接受 > 100 MB 的檔案（push 會被拒），
      Release 附件每個上限 2 GB。所以大檔必須切成 ≤2 GB 的分割檔。

    產出（預設 OutDir）：
      images.tar.gz.part00, part01, ...    ← 所有 Docker image
      ollama_models.tar.part00, ...        ← 只有加 -IncludeModels 才有
      SHA256SUMS.txt                       ← 每個分割檔的校驗碼
      manifest.json                        ← 版本、image 清單、切割資訊
      install-offline.ps1                  ← 安裝端用的腳本（自動複製過來）

.PARAMETER OutDir
    產出位置。**不要放在 repo 資料夾裡**，也不要放 C 槽。

.PARAMETER IncludeModels
    一併打包 11 GB 的 Ollama 模型。
    預設不含 —— ollama 官方 CDN 通常夠快，而多這 11 GB 會讓分割檔多一倍。
    目標機器完全沒有外網時才需要。

.PARAMETER PartSizeMB
    每個分割檔大小，預設 1900（GitHub Release 上限是 2 GB）。

.EXAMPLE
    .\scripts\make-offline-bundle.ps1 -OutDir E:\bundle
    .\scripts\make-offline-bundle.ps1 -OutDir E:\bundle -IncludeModels
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [switch]$IncludeModels,

    [ValidateRange(100, 2000)]
    [int]$PartSizeMB = 1900,

    [string]$Volume = 'real-project_ollama_data'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------------------
#  前置檢查
# ---------------------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "找不到 docker 指令。"
}
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Docker 沒有在執行，請先啟動 Docker Desktop。" }

if ($OutDir -like "$RepoRoot*") {
    throw "OutDir 不能放在 repo 資料夾裡（$RepoRoot）—— 幾 GB 的檔案誤 commit 會很難收拾。"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# ---------------------------------------------------------------------------
#  要打包哪些 image
#  從 compose 解析，才不會漏掉或寫死過期的清單。
# ---------------------------------------------------------------------------
Write-Host "解析 compose 的 image 清單..." -ForegroundColor Cyan

Push-Location $RepoRoot
try {
    $composeImages = docker compose config --images 2>$null
} finally {
    Pop-Location
}

if (-not $composeImages) {
    throw "無法從 docker compose config --images 取得清單。確認你在專案根目錄、且 .env 存在。"
}

$images = @($composeImages | Where-Object { $_ -and $_.Trim() } | Sort-Object -Unique)

# 確認每一個都在本機（不在就先拉下來，否則 docker save 會失敗）
foreach ($img in $images) {
    docker image inspect $img 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  本機沒有 $img，先 pull..." -ForegroundColor DarkGray
        docker pull $img
        if ($LASTEXITCODE -ne 0) { throw "pull 失敗：$img" }
    }
}

Write-Host "將打包以下 image：" -ForegroundColor Cyan
$images | ForEach-Object { Write-Host "  $_" }

# ---------------------------------------------------------------------------
#  工具函式
# ---------------------------------------------------------------------------
function Compress-File {
    param([string]$Source, [string]$Destination)

    Write-Host "壓縮 $(Split-Path -Leaf $Source) ..." -ForegroundColor Cyan
    $in  = [System.IO.File]::OpenRead($Source)
    try {
        $out = [System.IO.File]::Create($Destination)
        try {
            $gz = New-Object System.IO.Compression.GZipStream(
                $out, [System.IO.Compression.CompressionLevel]::Optimal)
            try { $in.CopyTo($gz, 1MB) } finally { $gz.Dispose() }
        } finally { $out.Dispose() }
    } finally { $in.Dispose() }
}

function Split-File {
    param([string]$Source, [int]$ChunkMB)

    $chunk = $ChunkMB * 1MB
    $total = (Get-Item $Source).Length
    $count = [math]::Ceiling($total / $chunk)

    if ($count -le 1) {
        Write-Host "  $(Split-Path -Leaf $Source) 小於單檔上限，不切割" -ForegroundColor DarkGray
        return @(Get-Item $Source)
    }

    Write-Host "切割 $(Split-Path -Leaf $Source) 成 $count 份 ..." -ForegroundColor Cyan

    $parts  = @()
    $stream = [System.IO.File]::OpenRead($Source)
    try {
        $buffer = New-Object byte[] 1MB
        for ($i = 0; $i -lt $count; $i++) {
            $partPath = "{0}.part{1:d2}" -f $Source, $i
            $written  = 0L
            $outFile  = [System.IO.File]::Create($partPath)
            try {
                while ($written -lt $chunk) {
                    $want = [math]::Min($buffer.Length, $chunk - $written)
                    $read = $stream.Read($buffer, 0, $want)
                    if ($read -le 0) { break }
                    $outFile.Write($buffer, 0, $read)
                    $written += $read
                }
            } finally { $outFile.Dispose() }
            $parts += Get-Item $partPath
            Write-Host ("  {0}  {1:N0} MB" -f (Split-Path -Leaf $partPath), ($written / 1MB))
        }
    } finally { $stream.Dispose() }

    Remove-Item $Source -Force
    return $parts
}

# ---------------------------------------------------------------------------
#  1. docker save
# ---------------------------------------------------------------------------
$imagesTar = Join-Path $OutDir 'images.tar'
Write-Host "`n[1/4] docker save（幾 GB，需要數分鐘）..." -ForegroundColor Yellow
docker save @images -o $imagesTar
if ($LASTEXITCODE -ne 0) { throw "docker save 失敗。" }
Write-Host ("  images.tar  {0:N2} GB" -f ((Get-Item $imagesTar).Length / 1GB))

# docker save 的輸出是未壓縮的，gzip 通常能省下一半以上。
$imagesGz = "$imagesTar.gz"
Write-Host "`n[2/4] 壓縮..." -ForegroundColor Yellow
Compress-File -Source $imagesTar -Destination $imagesGz
Remove-Item $imagesTar -Force
Write-Host ("  images.tar.gz  {0:N2} GB" -f ((Get-Item $imagesGz).Length / 1GB))

$artifacts = @()
$artifacts += Split-File -Source $imagesGz -ChunkMB $PartSizeMB

# ---------------------------------------------------------------------------
#  2. Ollama 模型（選用）
# ---------------------------------------------------------------------------
if ($IncludeModels) {
    Write-Host "`n[3/4] 匯出 Ollama 模型（約 11 GB）..." -ForegroundColor Yellow
    $modelsTar = Join-Path $OutDir 'ollama_models.tar'

    # gguf 權重本來就壓過了，不再壓縮，直接切割。
    docker run --rm -v "${Volume}:/data" -v "${OutDir}:/backup" `
        alpine tar cf /backup/ollama_models.tar -C /data .
    if ($LASTEXITCODE -ne 0) { throw "模型匯出失敗。" }

    Write-Host ("  ollama_models.tar  {0:N2} GB" -f ((Get-Item $modelsTar).Length / 1GB))
    $artifacts += Split-File -Source $modelsTar -ChunkMB $PartSizeMB
} else {
    Write-Host "`n[3/4] 略過模型（沒有 -IncludeModels）" -ForegroundColor DarkGray
    Write-Host "      安裝端會由 ollama-model-init 自行下載 11 GB。" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------------------
#  3. 校驗碼與 manifest
# ---------------------------------------------------------------------------
Write-Host "`n[4/4] 產生校驗碼與 manifest..." -ForegroundColor Yellow

$sumFile = Join-Path $OutDir 'SHA256SUMS.txt'
Remove-Item $sumFile -ErrorAction SilentlyContinue
foreach ($a in $artifacts) {
    $h = (Get-FileHash $a.FullName -Algorithm SHA256).Hash.ToLower()
    "$h  $($a.Name)" | Add-Content -Path $sumFile -Encoding ascii
}

Push-Location $RepoRoot
try { $gitRev = (git rev-parse --short HEAD 2>$null) } catch { $gitRev = 'unknown' }
finally { Pop-Location }

@{
    created_at     = (Get-Date).ToString('o')
    git_commit     = "$gitRev"
    includes_models = [bool]$IncludeModels
    part_size_mb   = $PartSizeMB
    images         = $images
    artifacts      = @($artifacts | ForEach-Object { $_.Name })
} | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $OutDir 'manifest.json') -Encoding UTF8

# 安裝端要用的東西一起放進去
Copy-Item (Join-Path $PSScriptRoot 'install-offline.ps1') $OutDir -Force
$doc = Join-Path $RepoRoot 'OFFLINE-INSTALL.md'
if (Test-Path $doc) { Copy-Item $doc $OutDir -Force }

# ---------------------------------------------------------------------------
Write-Host "`n=== 完成 ===" -ForegroundColor Green
Get-ChildItem $OutDir | Select-Object Name, @{n='MB';e={[math]::Round($_.Length/1MB)}} | Format-Table -AutoSize

$totalGB = [math]::Round((Get-ChildItem $OutDir | Measure-Object Length -Sum).Sum / 1GB, 2)
Write-Host "總計 $totalGB GB，位於 $OutDir" -ForegroundColor Green
Write-Host @"

上傳到 GitHub Release：

    cd "$RepoRoot"
    gh release create offline-v1.0.0 (Get-ChildItem "$OutDir\*" -File) ``
      --title "離線安裝包 v1.0.0" ``
      --notes-file OFFLINE-INSTALL.md

沒裝 gh CLI 的話，到 repo 的 Releases 頁面手動建立並把 $OutDir 裡的檔案全部拖進去。
"@ -ForegroundColor Cyan
