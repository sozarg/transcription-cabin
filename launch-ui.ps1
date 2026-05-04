$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $scriptRoot ".venv\Scripts\python.exe"
$app = Join-Path $scriptRoot "app_ui.py"
$url = "http://127.0.0.1:7860"

$existing = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
    Start-Process $url | Out-Null
    exit 0
}

Set-Location $scriptRoot
& $python $app
exit $LASTEXITCODE
