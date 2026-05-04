param(
    [Parameter(Mandatory = $true)]
    [string]$Url,
    [string]$OutputDir = "downloads"
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $scriptRoot ".venv\Scripts\python.exe"
$targetDir = Join-Path $scriptRoot $OutputDir

New-Item -ItemType Directory -Path $targetDir -Force | Out-Null

& $python -m yt_dlp `
    --no-playlist `
    -f bestaudio `
    -o (Join-Path $targetDir "%(title)s [%(id)s].%(ext)s") `
    $Url

exit $LASTEXITCODE
