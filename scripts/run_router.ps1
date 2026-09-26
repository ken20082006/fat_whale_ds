# Router 的看守程序。
#
# 跟 run_forever.ps1 同一個做法，但讀 `.env.router` ——
# Router 用嘅 bot token 同大肥鯨唔同（測試期用 beta bot），
# 仲要 FW_HERMES_KEY，唔想同大肥鯨嗰個 .env 混埋。
#
# 用法（在專案根目錄）：
#   powershell -ExecutionPolicy Bypass -File scripts\run_router.ps1
#
# ⚠️ 同一個 bot token 只可以有一個 process 揸住。Router 行緊嘅話，
#    大肥鯨唔可以用同一條 token（會 409）。

$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)

$envFile = ".env.router"
if (-not (Test-Path $envFile)) {
    Write-Host "搵唔到 $envFile。由 .env.router.example 複製一份再填。" -ForegroundColor Red
    exit 1
}

# 讀 .env.router 入 process 環境。註釋同空行跳過。
Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    Set-Item -Path ("Env:" + $line.Substring(0, $idx).Trim()) `
              -Value $line.Substring($idx + 1).Trim()
}

# ⚠️ 呢部機嘅 .venv 係另一部機（ckfung）嘅，用唔到。
#    如果 .venv 唔 work，改用本機嘅 python：
#        $python = "python"
$python = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "搵唔到 $python —— 改用系統 python。" -ForegroundColor Yellow
    $python = "python"
}

Write-Host "Router 看守程序啟動。日誌：$env:FW_LOG_DIR" -ForegroundColor Cyan
Write-Host "Hermes：$env:FW_HERMES_URL" -ForegroundColor Cyan

while ($true) {
    $started = Get-Date
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 啟動 Router…" -ForegroundColor Green

    & $python -m dafeijing.router.main

    $code = $LASTEXITCODE
    $ran = [int]((Get-Date) - $started).TotalSeconds

    if ($ran -lt 60) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 只跑了 $ran 秒就結束（exit $code），30 秒後重試" -ForegroundColor Yellow
        Start-Sleep -Seconds 30
    } else {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 結束（exit $code，跑了 $ran 秒），5 秒後重啟" -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
}
