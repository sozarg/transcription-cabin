param(
    [Parameter(Mandatory = $true)]
    [string]$Url,
    [string]$OutputDir = "downloads"
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $scriptRoot ".venv\Scripts\python.exe"
$targetDir = Join-Path $scriptRoot $OutputDir
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

Import-DotEnvFile -Path (Join-Path $scriptRoot ".env")

New-Item -ItemType Directory -Path $targetDir -Force | Out-Null

& $python -m yt_dlp `
    --no-playlist `
    -f bestaudio `
    -o (Join-Path $targetDir "%(title)s [%(id)s].%(ext)s") `
    $Url

exit $LASTEXITCODE
