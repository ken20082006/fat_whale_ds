# 本地測試用的看守程序。
#
# 家用網路偶爾會抖動，bot 在啟動階段連不上 Telegram 就會直接結束。
# 正式部署由 systemd 的 Restart=always 負責，這裡只是本地版的替代品。
#
# 用法（在專案根目錄）：
#   powershell -ExecutionPolicy Bypass -File scripts\run_forever.ps1
#
# 停止：關掉那個視窗，或 taskkill 對應的 PID。

$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot -Parent)

$python = ".\.venv\Scripts\python.exe"
$log = "logs\fatwhale.log"

Write-Host "看守程序啟動。日誌：$log" -ForegroundColor Cyan

while ($true) {
    $started = Get-Date
    Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 啟動 bot…" -ForegroundColor Green

    & $python -m dafeijing.main

    $code = $LASTEXITCODE
    $ran = [int]((Get-Date) - $started).TotalSeconds

    # 跑不到 60 秒就結束，多半是啟動時的網路問題。退避久一點再試，
    # 免得連不上時瘋狂重啟把日誌灌爆。
    if ($ran -lt 60) {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 只跑了 $ran 秒就結束（exit $code），30 秒後重試" -ForegroundColor Yellow
        Start-Sleep -Seconds 30
    } else {
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] 結束（exit $code，跑了 $ran 秒），5 秒後重啟" -ForegroundColor Yellow
        Start-Sleep -Seconds 5
    }
}
