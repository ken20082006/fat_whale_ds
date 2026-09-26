# Router 的看守程序。
#
# 讀一個 `.env.router` —— Router 用嘅 bot token 同大肥鯨唔同，
# 仲要 FW_HERMES_KEY，唔想同大肥鯨嗰個 .env 混埋。
#
# 用法（在專案根目錄）：
#   powershell -ExecutionPolicy Bypass -File scripts\run_router.ps1
#
# 行第二隻 bot（貝爾法斯特）—— **一定要傳埋 -WorkDir**，佢唔可以讀到
# 大肥鯨嗰個 `.env`：
#   powershell -ExecutionPolicy Bypass -File scripts\run_router.ps1 `
#       -EnvPath belfast\.env.router -WorkDir belfast
#
# ## 為什麼 CWD 決定繼唔繼承
#
# `settings.py` 寫死 `env_file=".env"`，係 **CWD 相對**。process 環境蓋過
# `.env`，但**冇填嘅欄位會靜靜哋食 `.env` 嗰個值**。
#
# - 大肥鯨：CWD = repo 根 → 讀 repo `.env`（佢自己嗰份），正確。
# - 貝爾法斯特：CWD = `belfast/` → 嗰度冇 `.env` → **零繼承**。
#   佢要用嘅每一個 `FW_` 都要喺自己嗰份 env 檔填齊。
#
# 呢個係刻意嘅：兩隻 bot 係兩套嘢，唔應該透過 `.env` 互相影響 ——
# 尤其係 token 同 API key，繼承到就大件事。
#
# ⚠️ 兩份 env 檔入面嘅**相對路徑**（FW_DB_PATH、FW_LOG_DIR、
#    FW_HERMES_MEDIA_DIR）係由**各自嘅 CWD** 數起，唔係 repo 根。
#
# ⚠️ 同一個 bot token 只可以有一個 process 揸住。Router 行緊嘅話，
#    大肥鯨唔可以用同一條 token（會 409）。

param(
    [string]$EnvPath = ".env.router",
    [string]$WorkDir = ""
)

$ErrorActionPreference = "Continue"
$repoRoot = (Split-Path $PSScriptRoot -Parent)

# ── 以下兩樣一律以 repo 根為基準解析 ──────────────────
# 一定要喺 Set-Location **之前**做 —— 之後 CWD 未必係 repo 根。

# env 檔：唔填就係 repo 根嗰個 `.env.router`（大肥鯨），同以前一樣。
if (-not [System.IO.Path]::IsPathRooted($EnvPath)) {
    $EnvPath = Join-Path $repoRoot $EnvPath
}
if (-not (Test-Path $EnvPath)) {
    Write-Host "搵唔到 $EnvPath。由 .env.router.example 複製一份再填。" -ForegroundColor Red
    exit 1
}

# ⚠️ 呢部機嘅 .venv 係另一部機（ckfung）嘅，用唔到。
#    如果 .venv 唔 work，改用本機嘅 python：
#        $python = "python"
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "搵唔到 $python —— 改用系統 python。" -ForegroundColor Yellow
    $python = "python"
}

# ── 轉 CWD（決定讀邊個 .env）──────────────────────────
# 唔傳 -WorkDir 就係 repo 根 —— 大肥鯨行為完全不變。
$runDir = if ($WorkDir -eq "") { $repoRoot } else { Join-Path $repoRoot $WorkDir }
if (-not (Test-Path $runDir)) {
    Write-Host "搵唔到工作目錄 $runDir。" -ForegroundColor Red
    exit 1
}
Set-Location $runDir

# 代碼（dafeijing/）喺 repo 根。CWD 唔係 repo 根嘅話，
# `-m dafeijing.router.main` 就 import 唔到 —— 要靠 PYTHONPATH 搭返。
$env:PYTHONPATH = $repoRoot

# 讀 env 檔入 process 環境。註釋同空行跳過。
# process 環境蓋過 `.env`，所以呢度填咗嘅，CWD 嗰個 `.env` 蓋唔返。
#
# ⚠️ **`-Encoding UTF8` 唔可以刪。** Windows PowerShell 5.1 讀冇 BOM 嘅檔
#    會當成系統 ANSI（呢部機係 CP950）。檔入面有中文註釋，UTF-8 位元組被
#    當 Big5 解碼時會出現「前導位元組食咗下一個位元組」——**連換行都會被吞**，
#    於是行數錯亂，實測 18 條只讀到 6 條（`FW_TELEGRAM_BOT_TOKEN` 就係
#    被吞嗰批之一，Router 起唔到）。
Get-Content $EnvPath -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if ($line -eq "" -or $line.StartsWith("#")) { return }
    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }
    Set-Item -Path ("Env:" + $line.Substring(0, $idx).Trim()) `
              -Value $line.Substring($idx + 1).Trim()
}

Write-Host "Router 看守程序啟動" -ForegroundColor Cyan
Write-Host "  env 檔：$EnvPath" -ForegroundColor Cyan
Write-Host "  CWD　：$runDir" -ForegroundColor Cyan
Write-Host "  日誌　：$env:FW_LOG_DIR" -ForegroundColor Cyan
Write-Host "  Hermes：$env:FW_HERMES_URL" -ForegroundColor Cyan

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
