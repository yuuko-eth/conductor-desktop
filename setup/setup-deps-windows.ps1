# Install deps for conductor-desktop on Windows 10/11.
# Run from an elevated or non-elevated PowerShell (non-elevated is fine for
# normal app automation; elevate only if you plan to drive admin apps).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File .\setup-deps-windows.ps1
#   # or, after "Set-ExecutionPolicy RemoteSigned -Scope CurrentUser":
#   .\setup-deps-windows.ps1

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Section($title) {
    Write-Host ""
    Write-Host ("=" * 64) -ForegroundColor Cyan
    Write-Host "  $title" -ForegroundColor Cyan
    Write-Host ("=" * 64) -ForegroundColor Cyan
    Write-Host ""
}

Write-Section "conductor-desktop setup for Windows"

# ---------------------------------------------------------------------------
# 1. Python
# ---------------------------------------------------------------------------
# Nothing to install: uv sync (step 3) downloads a Python that fits
# requires-python. A fresh Windows has a python.exe on PATH that only opens
# the Microsoft Store, so checking for one here would only mislead.

# ---------------------------------------------------------------------------
# 2. uv
# ---------------------------------------------------------------------------
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    Write-Host "Installing uv..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    # Refresh PATH for the current session so `uv` is immediately usable.
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","User") + ";" + $env:Path
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $uv) {
        Write-Host "uv installed but not on PATH in this session." -ForegroundColor Yellow
        Write-Host "  Open a new PowerShell window and re-run this script."
        exit 1
    }
}
Write-Host "uv: $((& uv --version))"

# ---------------------------------------------------------------------------
# 3. Python deps (pywinauto, pywin32, mcp, websockets, etc.)
# ---------------------------------------------------------------------------
Write-Section "Installing Python deps (uv sync)"
Push-Location $ScriptDir
try {
    & uv sync
} finally {
    Pop-Location
}

# Windows on Arm ships without the Visual C++ runtime's MFC DLL, and
# pywin32's win32ui (which pywinauto imports) won't load without it.
$arch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($arch -eq "ARM64" -and -not (Test-Path "$env:SystemRoot\System32\mfc140u.dll")) {
    Write-Section "Installing the Visual C++ runtime for Arm64"
    $redist = Join-Path $env:TEMP "vc_redist.arm64.exe"
    Invoke-WebRequest https://aka.ms/vs/17/release/vc_redist.arm64.exe -OutFile $redist
    # Asks for elevation when this window isn't already elevated.
    Start-Process $redist -ArgumentList "/install", "/quiet", "/norestart" -Verb RunAs -Wait
    if (-not (Test-Path "$env:SystemRoot\System32\mfc140u.dll")) {
        Write-Host "The Visual C++ runtime didn't install; pywinauto won't import." -ForegroundColor Yellow
        Write-Host "  Install it by hand: https://aka.ms/vs/17/release/vc_redist.arm64.exe"
    }
}

# ---------------------------------------------------------------------------
# 4. UIPI explanation
# ---------------------------------------------------------------------------
Write-Section "Permissions: Windows UIPI"
Write-Host "Windows blocks input injection across integrity levels."
Write-Host "The agent process must run at the SAME or HIGHER integrity"
Write-Host "level as the target application."
Write-Host ""
Write-Host "  Normal apps       -> run this process as normal user (OK)"
Write-Host "  Admin apps        -> run this process as Administrator"
Write-Host "  UAC dialogs       -> cannot be automated (by design)"
Write-Host ""
Write-Host "No upfront permission grants needed (unlike macOS TCC)."
Write-Host "UIPI failures surface at runtime with a descriptive error."

# ---------------------------------------------------------------------------
# 5. OpenSSH server (optional, for remote deployment / driving)
# ---------------------------------------------------------------------------
Write-Section "OpenSSH Server (optional)"
$ssh = Get-WindowsCapability -Online -Name "OpenSSH.Server*" -ErrorAction SilentlyContinue |
    Where-Object { $_.State -eq "Installed" }
if ($ssh) {
    Write-Host "OpenSSH.Server already installed."
} else {
    Write-Host "Not installed. To enable (needs Administrator):"
    Write-Host "  Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0"
    Write-Host "  Start-Service sshd"
    Write-Host "  Set-Service -Name sshd -StartupType Automatic"
    Write-Host "  New-NetFirewallRule -Name sshd -DisplayName 'OpenSSH Server' ``"
    Write-Host "    -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22"
}

# ---------------------------------------------------------------------------
# 6. Next steps
# ---------------------------------------------------------------------------
Write-Section "Next steps"
# uv sync puts the venv next to pyproject.toml, one level above this script.
$Venv = Join-Path (Split-Path -Parent $ScriptDir) ".venv"
Write-Host "Check capture and input, from the desktop or RDP (not SSH):"
Write-Host "  $Venv\Scripts\conductor-desktop-check.exe"
Write-Host ""
Write-Host "Add it to Claude Code:"
Write-Host "  claude mcp add --scope user conductor-desktop -- `"$Venv\Scripts\conductor-desktop.exe`""
Write-Host ""
Write-Host "Any other MCP client: command = $Venv\Scripts\conductor-desktop.exe"
Write-Host "(in JSON, double every backslash). A grounder is optional; to add"
Write-Host "one, set CONDUCTOR_DESKTOP_GROUNDER_URL (e.g. http://127.0.0.1:8080)"
Write-Host "in the client's env."
Write-Host ""
Write-Host "Done." -ForegroundColor Green
