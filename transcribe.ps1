param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,
    [string]$OutputDir = "transcripts",
    [string]$Model = "large-v3",
    [string]$Language = "es",
    [switch]$Cpu,
    [switch]$WordTimestamps,
    [switch]$NoVad,
    [ValidateSet("transcribe", "translate")]
    [string]$Task = "transcribe"
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $scriptRoot ".venv\Scripts\python.exe"
$script = Join-Path $scriptRoot "scripts\transcribe.py"
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

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"

$device = if ($Cpu) { "cpu" } else { "auto" }
$nvidiaBins = @(
    (Join-Path $scriptRoot ".venv\Lib\site-packages\nvidia\cublas\bin"),
    (Join-Path $scriptRoot ".venv\Lib\site-packages\nvidia\cudnn\bin"),
    (Join-Path $scriptRoot ".venv\Lib\site-packages\nvidia\cuda_nvrtc\bin")
) | Where-Object { Test-Path $_ }

if ($nvidiaBins.Count -gt 0) {
    $env:PATH = (($nvidiaBins -join ";") + ";" + $env:PATH)
}

$argsList = @(
    $script
    "--input", $FilePath
    "--output-dir", $OutputDir
    "--model", $Model
    "--language", $Language
    "--device", $device
    "--task", $Task
)

if ($WordTimestamps) {
    $argsList += "--word-timestamps"
}

if ($NoVad) {
    $argsList += "--no-vad"
}

& $python @argsList
exit $LASTEXITCODE
