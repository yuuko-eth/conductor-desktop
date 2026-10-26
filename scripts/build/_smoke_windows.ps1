$ErrorActionPreference = "Continue"
$exe = "C:\Users\yuuko\conductor-mcp\dist\conductor-mcp.exe"
$env:CONDUCTOR_DESKTOP_GROUNDER_URL = "http://127.0.0.1:8933"

if (-not (Test-Path $exe)) {
    Write-Host "FAIL: $exe not found"
    exit 1
}

$size = (Get-Item $exe).Length / 1MB
Write-Host ("exe size: {0:N1} MB" -f $size)

$errLog = "C:\Users\yuuko\conductor-mcp\smoke.err.log"
$outLog = "C:\Users\yuuko\conductor-mcp\smoke.out.log"
Remove-Item $errLog,$outLog -ErrorAction SilentlyContinue

Write-Host ">> launching exe..."
$p = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden -RedirectStandardError $errLog -RedirectStandardOutput $outLog
Write-Host "PID=$($p.Id)"

Start-Sleep -Seconds 10

$proc = Get-Process -Id $p.Id -ErrorAction SilentlyContinue
if ($proc) {
    Write-Host "ALIVE after 10s"
    Write-Host ("working_set: {0:N1} MB" -f ($proc.WorkingSet64 / 1MB))
    Write-Host ("threads: {0}" -f $proc.Threads.Count)
    Stop-Process -Id $p.Id -Force
    Start-Sleep -Seconds 2
    Write-Host "stopped"
} else {
    Write-Host "DEAD before 10s (exit code $($p.ExitCode))"
}

Write-Host "---STDERR (first 80 lines)---"
Get-Content $errLog -ErrorAction SilentlyContinue -TotalCount 80
Write-Host "---STDOUT (first 80 lines)---"
Get-Content $outLog -ErrorAction SilentlyContinue -TotalCount 80
