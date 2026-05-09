param(
    [Parameter(Mandatory = $false)]
    [string]$Token
)

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$envPath = Join-Path $scriptRoot ".env"

if (-not $Token) {
    $secure = Read-Host "Paste your Hugging Face token (hf_...)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $Token = [Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

if (-not $Token -or -not $Token.StartsWith("hf_")) {
    Write-Error "Invalid token. It should start with hf_."
    exit 1
}

$lines = @()
if (Test-Path $envPath) {
    $lines = Get-Content $envPath | Where-Object {
        $_ -notmatch '^\s*HF_TOKEN\s*=' -and $_ -notmatch '^\s*HUGGINGFACE_HUB_TOKEN\s*='
    }
}

$lines += "HF_TOKEN=$Token"
$lines += "HUGGINGFACE_HUB_TOKEN=$Token"

Set-Content -Path $envPath -Value $lines -Encoding UTF8
Write-Output "Saved token in $envPath"
Write-Output "Restart the UI (or rerun your CLI command) to apply it."
