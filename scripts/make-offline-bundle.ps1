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
    .\scripts\make-offline-bundle.ps1 -OutDir E:\bundle -IncludeModels -ModelsTar E:\docker-backup\ollama_models.tar

.NOTES
    本檔必須以 UTF-8 with BOM 儲存。Windows PowerShell 5.1 沒有 BOM 時
    會用系統編碼（繁中版是 Big5）解析，中文變亂碼並導致語法錯誤。
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [switch]$IncludeModels,

    # 已經用 ollama-models.ps1 匯出過的話，指到那個檔就不必重新匯出一次。
    [string]$ModelsTar,

    [ValidateRange(100, 2000)]
    [int]$PartSizeMB = 1900,

    # 壓縮 images.tar。實測只省約 0.7%（docker save 的層本來就壓過了），
    # 卻要多花好幾分鐘，所以預設關閉。
    [switch]$Compress,

    [string]$Volume = 'real-project_ollama_data'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------------------
#  $ErrorActionPreference = 'Stop' 會把原生指令寫到 stderr 的訊息當成中止錯誤。
#  「這個 image 在不在」這種探測本來就會輸出 stderr，所以要隔離開來，
#  只看離開碼。
# ---------------------------------------------------------------------------
function Test-NativeOk {
    param([Parameter(Mandatory)][string]$FilePath,
          [string[]]$ArgumentList = @())

    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $FilePath @ArgumentList 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } finally {
        $ErrorActionPreference = $old
    }
}

# ---------------------------------------------------------------------------
#  前置檢查
# ---------------------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "找不到 docker 指令。"
}
if (-not (Test-NativeOk docker @('info'))) {
    throw "Docker 沒有在執行，請先啟動 Docker Desktop。"
}

if ($OutDir -like "$RepoRoot*") {
    throw "OutDir 不能放在 repo 資料夾裡（$RepoRoot）—— 幾 GB 的檔案誤 commit 會很難收拾。"
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# 清掉上一次的產出。新舊分割檔混在同一個資料夾會讓安裝端合併出壞檔。
$stale = @(Get-ChildItem $OutDir -File -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -match '^(images\.tar(\.gz)?|ollama_models\.tar)(\.part\d+)?$' -or
                          $_.Name -in @('SHA256SUMS.txt','manifest.json') })
if ($stale.Count -gt 0) {
    Write-Host "清除 $OutDir 內上一次的產出（$($stale.Count) 個檔案）..." -ForegroundColor DarkGray
    $stale | Remove-Item -Force
}

# ---------------------------------------------------------------------------
#  要打包哪些 image
#  從 compose 解析，才不會漏掉或寫死過期的清單。
# ---------------------------------------------------------------------------
Write-Host "解析 compose 的 image 清單..." -ForegroundColor Cyan

Push-Location $RepoRoot
try {
    $composeImages = docker compose config --images 2>$null
    if (-not $composeImages) {
        throw "無法取得 image 清單。確認你在專案根目錄、且 .env 存在。"
    }
    $images = @($composeImages | Where-Object { $_ -and $_.Trim() } | Sort-Object -Unique)

    # 有 build: 區塊的服務是本機自製的，不存在於任何 registry，
    # 只能 build 不能 pull。其餘才是上游 image。
    $builtImages = @()
    try {
        $cfg = docker compose config --format json 2>$null | ConvertFrom-Json
        foreach ($svc in $cfg.services.PSObject.Properties) {
            if ($svc.Value.build -and $svc.Value.image) {
                $builtImages += $svc.Value.image
            }
        }
    } catch {
        Write-Host "  (無法解析 build 區塊，將全部視為上游 image)" -ForegroundColor DarkGray
    }
    $builtImages = @($builtImages | Sort-Object -Unique)

    # --- 自製 image：直接建置，順便確保標籤與目前的 compose 一致 ---
    if ($builtImages.Count -gt 0) {
        Write-Host "`n建置自製 image（幾分鐘）..." -ForegroundColor Yellow
        $builtImages | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
        docker compose build
        if ($LASTEXITCODE -ne 0) { throw "docker compose build 失敗。" }
    }

    # --- 上游 image：本機沒有才 pull ---
    foreach ($img in $images) {
        if ($builtImages -contains $img) { continue }
        if (-not (Test-NativeOk docker @('image', 'inspect', $img))) {
            Write-Host "  本機沒有 $img，先 pull..." -ForegroundColor DarkGray
            docker pull $img
            if ($LASTEXITCODE -ne 0) { throw "pull 失敗：$img" }
        }
    }

    # --- 最後確認全部就位 ---
    $missing = @($images | Where-Object { -not (Test-NativeOk docker @('image', 'inspect', $_)) })
    if ($missing.Count -gt 0) {
        throw "下列 image 仍不存在，無法打包：`n  $($missing -join "`n  ")"
    }
} finally {
    Pop-Location
}

Write-Host "`n將打包以下 image：" -ForegroundColor Cyan
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

$imagesFinal = $imagesTar
if ($Compress) {
    $imagesGz = "$imagesTar.gz"
    Write-Host "`n[2/4] 壓縮..." -ForegroundColor Yellow
    Compress-File -Source $imagesTar -Destination $imagesGz
    Remove-Item $imagesTar -Force
    $imagesFinal = $imagesGz
    Write-Host ("  images.tar.gz  {0:N2} GB" -f ((Get-Item $imagesGz).Length / 1GB))
} else {
    Write-Host "`n[2/4] 略過壓縮（docker save 的層已是壓縮狀態，實測只省約 0.7%）" -ForegroundColor DarkGray
    Write-Host "      要壓的話加 -Compress。" -ForegroundColor DarkGray
}

$artifacts = @()
$artifacts += Split-File -Source $imagesFinal -ChunkMB $PartSizeMB

# ---------------------------------------------------------------------------
#  2. Ollama 模型（選用）
# ---------------------------------------------------------------------------
if ($IncludeModels) {
    # ⚠️ PowerShell 的變數名稱不分大小寫，所以這裡絕對不能叫 $modelsTar ——
    #    那會直接覆蓋掉參數 $ModelsTar。
    $modelsDest = Join-Path $OutDir 'ollama_models.tar'

    if ($ModelsTar) {
        # 已經匯出過，直接複製，省下重新打包 11 GB 的時間。
        if (-not (Test-Path $ModelsTar)) { throw "找不到 $ModelsTar" }
        Write-Host "`n[3/4] 複製既有的模型匯出檔（11 GB，需要數分鐘）..." -ForegroundColor Yellow
        Write-Host "  來源 $ModelsTar" -ForegroundColor DarkGray
        Copy-Item -LiteralPath $ModelsTar -Destination $modelsDest -Force
    } else {
        Write-Host "`n[3/4] 匯出 Ollama 模型（約 11 GB）..." -ForegroundColor Yellow
        # gguf 權重本來就壓過了，不再壓縮，直接切割。
        docker run --rm -v "${Volume}:/data" -v "${OutDir}:/backup" `
            alpine tar cf /backup/ollama_models.tar -C /data .
        if ($LASTEXITCODE -ne 0) { throw "模型匯出失敗。" }
    }

    Write-Host ("  ollama_models.tar  {0:N2} GB" -f ((Get-Item $modelsDest).Length / 1GB))
    $artifacts += Split-File -Source $modelsDest -ChunkMB $PartSizeMB
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
