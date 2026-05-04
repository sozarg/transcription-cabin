$listener = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $listener) {
    Write-Output "No hay una UI escuchando en el puerto 7860."
    exit 0
}

Stop-Process -Id $listener.OwningProcess -Force
Write-Output "UI detenida. PID: $($listener.OwningProcess)"
