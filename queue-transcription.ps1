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

$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$transcribeScript = Join-Path $scriptRoot "transcribe.ps1"
$logsRoot = Join-Path $scriptRoot "logs"

function Quote-PowerShellArgument {
    param([string]$Value)
    return "'" + ($Value -replace "'", "''") + "'"
}

function New-SafeName {
    param([string]$Value)
    $safe = [IO.Path]::GetFileNameWithoutExtension($Value) -replace "[^\p{L}\p{Nd}._-]+", "-"
    $safe = $safe.Trim("-")
    if (-not $safe) {
        return "transcription"
    }
    return $safe
}

if (-not (Test-Path -LiteralPath $transcribeScript)) {
    throw "No se encontro transcribe.ps1 en $scriptRoot"
}

$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$jobName = New-SafeName -Value $FilePath
$jobLogDir = Join-Path $logsRoot "queued-$timestamp-$jobName"
New-Item -ItemType Directory -Force -Path $jobLogDir | Out-Null

$stdoutLog = Join-Path $jobLogDir "stdout.log"
$stderrLog = Join-Path $jobLogDir "stderr.log"

$commandParts = @(
    "&",
    (Quote-PowerShellArgument $transcribeScript),
    "-FilePath", (Quote-PowerShellArgument $FilePath),
    "-OutputDir", (Quote-PowerShellArgument $OutputDir),
    "-Model", (Quote-PowerShellArgument $Model),
    "-Language", (Quote-PowerShellArgument $Language),
    "-Task", (Quote-PowerShellArgument $Task)
)

if ($Cpu) {
    $commandParts += "-Cpu"
}
if ($WordTimestamps) {
    $commandParts += "-WordTimestamps"
}
if ($NoVad) {
    $commandParts += "-NoVad"
}

$command = $commandParts -join " "
$process = Start-Process powershell `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) `
    -WorkingDirectory $scriptRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutLog `
    -RedirectStandardError $stderrLog `
    -PassThru

[pscustomobject]@{
    ProcessId = $process.Id
    Input = $FilePath
    Stdout = $stdoutLog
    Stderr = $stderrLog
    LogDirectory = $jobLogDir
}
