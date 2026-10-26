# Build conductor-desktop as a Windows single-file binary using Nuitka.
#
# TEMPLATE -- not yet verified end-to-end on Windows. The macOS build
# (nuitka_macos.sh) is the proven reference.
#
# Output: dist\conductor-desktop.exe  (single file via --onefile)
# Build time estimate: 15-25min depending on host CPU.
#
# Usage:
#   scripts\build\nuitka_windows.ps1
#   $env:SLIM=1; scripts\build\nuitka_windows.ps1     # drop imagehash
#
# Requires: Windows 10+ x64, Python 3.12+ (https://www.python.org/),
# uv (https://docs.astral.sh/uv/), Visual Studio Build Tools 2022 or
# MinGW64 + clang. Run from conductor-desktop\.
#
# Notes:
#   - pywin32 ships compiled DLLs (pythoncom*, pywintypes*); Nuitka's
#     pywin32 plugin auto-includes them -- listed below for safety.
#   - comtypes generates COM type-library wrappers at runtime in
#     `comtypes.gen.*`. We pre-warm that cache during install so Nuitka
#     can freeze whatever pywinauto needs at build time.
#   - pystray on Windows uses pywin32 (no separate native deps).
# Don't use $ErrorActionPreference = "Stop" globally: on PowerShell 5.1
# (default Windows 10) Stop turns any stderr write from a native command
# into NativeCommandError, killing the script. uv ("Using CPython..."),
# pywinauto ("Revert to STA COM..."), and Nuitka all routinely write
# informational lines to stderr. Stay on Continue and check $LASTEXITCODE
# explicitly after each native call.
$ErrorActionPreference = "Continue"

function Invoke-Native {
    param([string]$Name)
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: $Name failed with exit code $LASTEXITCODE"
        exit $LASTEXITCODE
    }
}

# Move to conductor-desktop/ regardless of where this is launched from.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location (Resolve-Path "$scriptDir\..\..")

$OutDir = if ($env:OUT_DIR) { $env:OUT_DIR } else { "dist" }
$Py     = if ($env:PY)      { $env:PY }      else { "python" }
$Entry  = "scripts\build\conductor_desktop_entry.py"

if (-not (Test-Path $Entry)) {
    Write-Host "ERROR: missing build shim: $Entry"
    exit 1
}

# 1. Build venv.
$BuildVenv = if ($env:BUILD_VENV) { $env:BUILD_VENV } else { "$env:TEMP\conductor-desktop-build-venv" }
if (-not (Test-Path "$BuildVenv\Scripts\python.exe")) {
    Write-Host ">> creating build venv at $BuildVenv"
    uv venv --python $Py $BuildVenv
    Invoke-Native "uv venv"
}
$VenvPy = "$BuildVenv\Scripts\python.exe"
uv pip install --python $VenvPy -q nuitka ordered-set zstandard
Invoke-Native "uv pip install (nuitka)"
uv pip install --python $VenvPy -q -e ".[windows]"
Invoke-Native "uv pip install (.[windows])"

# 1b. Pre-generate comtypes COM wrappers so Nuitka can freeze them.
# Pywinauto pulls UIA / accessibility type-libs lazily; running this
# import once warms `comtypes.gen` with the needed Python modules.
& $VenvPy -W ignore -c "import comtypes.client; import pywinauto"
Invoke-Native "comtypes pre-warm"

# 2. Nuitka args.
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
$Args = @(
    "--onefile",
    "--standalone",
    "--output-dir=$OutDir",
    "--output-filename=conductor-desktop.exe",
    "--assume-yes-for-downloads",
    "--include-package=conductor_desktop",
    "--include-package-data=conductor_desktop",   # tray PNGs (assets/) + dashboard static
    "--include-package=mcp",
    # pywin32 (win32api/con/gui/clipboard, pywintypes, pythoncom) is shipped
    # as .pyd binaries -- NOT importable Python packages. Listing them as
    # --include-package= makes Nuitka try to compile the .pyd as source and
    # FATAL out ("can't open file 'win32file.pyd'"). Nuitka's built-in
    # pywin32 plugin (auto-enabled) handles them correctly via import-follow.
    "--include-package=pywinauto",
    "--include-package=comtypes",
    "--include-package=comtypes.gen",
    "--nofollow-import-to=tkinter",
    "--nofollow-import-to=test",
    "--nofollow-import-to=unittest",
    "--nofollow-import-to=pytest",
    # Skip PIL.TiffImagePlugin: Nuitka emits a 110k-line C TU that crashes
    # gcc 15.2.0's IPA fnsummary pass on MinGW64. PNG/JPEG plugins still
    # ship; TIFF isn't used by conductor-desktop's screenshot path.
    "--nofollow-import-to=PIL.TiffImagePlugin",
    "--remove-output",
    "--no-progressbar",
    "--windows-console-mode=disable",  # tray daemon, no flashing console
    # Use MSVC (VS 2022 Build Tools). Nuitka's auto-downloaded MinGW64
    # gcc 15.2.0 ICEs on multiple TUs in this codebase (PIL, anyio,
    # click). MSVC is Nuitka's most-tested Windows backend.
    "--msvc=latest",
    # Disable clcache. When clcache is present, Nuitka wraps cl with it and
    # clcache runs its own preprocessor pass to compute the cache key. That
    # pass cannot resolve Nuitka's GENERATED headers (nuitka/blobs.h,
    # *_generated.h, comparisons_*.h, operations_*.h) which Nuitka emits into
    # the scons build dir (/I.) during the build -- every TU then dies with
    # "clcache: preprocessor failed" + "C1083: Cannot open include file".
    # We don't reuse a compiler cache across these one-shot builds anyway.
    "--disable-ccache"
)

if ($env:SLIM -eq "1") {
    $Args += "--nofollow-import-to=imagehash"
    $Args += "--nofollow-import-to=scipy"
    $Args += "--nofollow-import-to=pywt"
}

if ($env:ICON) {
    $Args += "--windows-icon-from-ico=$($env:ICON)"
}

$Args += "--main=$((Get-Location).Path)\$Entry"

Write-Host ">> compiling..."
& $VenvPy -m nuitka @Args
Invoke-Native "nuitka"

$Bin = "$OutDir\conductor-desktop.exe"
Write-Host ""
Write-Host ">> done: $Bin"
if (Test-Path $Bin) {
    "{0:N1} MB" -f ((Get-Item $Bin).Length / 1MB) | Write-Host
}

Write-Host ""
Write-Host "Smoke test:"
Write-Host "  `$env:CONDUCTOR_DESKTOP_GROUNDER_URL='http://your-vlm:8000'"
Write-Host "  .\$Bin"
Write-Host ""
Write-Host "Distribution:"
Write-Host "  Sign with signtool /fd sha256 /tr http://timestamp.digicert.com /td sha256 /a $Bin"
