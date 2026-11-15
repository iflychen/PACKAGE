param(
    [Parameter(Mandatory=$true)][string]$TargetDir
)
$ErrorActionPreference = 'Stop'
$src = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = (Resolve-Path -LiteralPath $TargetDir).Path
$files = @('app.py','Aniki.py','aniki_api.py','cmm_review_adapter.py','generic_rowreader.py','rowreader.py')
foreach ($name in @('app.py','Aniki.py','aniki_api.py','generic_rowreader.py','rowreader.py')) {
    if (-not (Test-Path -LiteralPath (Join-Path $target $name))) {
        throw "目標資料夾缺少 $name，請確認指定的是存放 pipelines 原始程式的目錄：$target"
    }
}
$timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$backup = Join-Path $target "backup_handwriting_review_$timestamp"
Write-Host "預計更新：$target" -ForegroundColor Cyan
Write-Host "將備份原程式至：$backup" -ForegroundColor Yellow
Write-Host "此腳本不會刪除或覆蓋 review_tasks.db。請先停止 pipelines 並備份實際掛載的 SQLite 檔案。" -ForegroundColor Yellow
$answer = Read-Host '輸入 YES 以備份並覆蓋六個程式檔'
if ($answer -ne 'YES') { Write-Host '已取消'; exit 1 }
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach ($name in $files) {
    $destination = Join-Path $target $name
    if (Test-Path -LiteralPath $destination) {
        Copy-Item -LiteralPath $destination -Destination (Join-Path $backup $name) -Force
    }
}
foreach ($name in $files) {
    Copy-Item -LiteralPath (Join-Path $src $name) -Destination (Join-Path $target $name) -Force
    Write-Host "已套用 $name" -ForegroundColor Green
}
Write-Host "套用完成。備份位置：$backup" -ForegroundColor Green
Write-Host '下一步：回到專案根目錄執行 docker compose build pipelines，然後 docker compose up -d pipelines'
