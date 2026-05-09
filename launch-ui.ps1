$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $scriptRoot ".venv\Scripts\python.exe"
$app = Join-Path $scriptRoot "app_ui.py"
$url = "http://127.0.0.1:7860"
if (-not (Test-Path $python)) {
    $python = "python"
}

function Import-DotEnvFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) {
        return
    }

    foreach ($line in Get-Content $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $parts = $trimmed -split "=", 2
        if ($parts.Count -ne 2) {
            continue
        }
        $key = $parts[0].Trim()
        $value = $parts[1].Trim().Trim("'").Trim('"')
        if ($key) {
            [Environment]::SetEnvironmentVariable($key, $value, "Process")
        }
    }
}

$existing = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) {
    Start-Process $url | Out-Null
    exit 0
}

Set-Location $scriptRoot
Import-DotEnvFile -Path (Join-Path $scriptRoot ".env")
& $python $app
exit $LASTEXITCODE
