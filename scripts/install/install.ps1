# conductor-desktop installer for Windows 10/11 (x64 and Arm64).
#
#   irm https://conductor-mcp.io/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://conductor-mcp.io/install.ps1))) -Client claude -Grounder managed -Gateway <url> -Token cgr_live_...
#
# `irm | iex` can't pass arguments; the scriptblock form can. Source-first: it
# installs uv, downloads the source (no git needed), runs
# setup\setup-deps-windows.ps1 (uv sync, plus the Arm64 VC++ runtime), checks
# capture and input when run at the desktop, and registers the server with
# your MCP client. Rerunning it updates in place and keeps .venv.
param(
  [string] $Dir = $(if ($env:CONDUCTOR_DESKTOP_HOME) { $env:CONDUCTOR_DESKTOP_HOME } else { Join-Path $env:LOCALAPPDATA 'conductor-desktop' }),
  [string] $Ref = $(if ($env:CONDUCTOR_DESKTOP_REF) { $env:CONDUCTOR_DESKTOP_REF } else { 'main' }),
  [string] $Source,                 # a checkout folder, a .zip/.tar.gz, or a URL to one
  [ValidateSet('', 'none', 'managed', 'self-host')] [string] $Grounder = '',
  [string] $Gateway = $env:CONDUCTOR_DESKTOP_GATEWAY_URL,
  [string] $Token = $env:CONDUCTOR_DESKTOP_API_KEY,
  [string] $Url = $env:CONDUCTOR_DESKTOP_GROUNDER_URL,
  [string] $Family = $env:CONDUCTOR_DESKTOP_GROUNDER_FAMILY,
  [string] $DescribeUrl = $env:CONDUCTOR_DESKTOP_DESCRIBE_URL,
  [string] $Client = '',            # claude, claude-desktop, pi, none (comma-separated)
  [switch] $NoCheck,
  [switch] $Yes
)
$ErrorActionPreference = 'Stop'

# The variables were CONDUCTOR_MCP_* before the rename. Carry any still set
# over to the new names (a set new name wins), then let them fill the
# parameters that neither a flag nor a CONDUCTOR_DESKTOP_* name set.
$fromEnv = @{ Dir = 'HOME'; Ref = 'REF'; Gateway = 'GATEWAY_URL'; Token = 'API_KEY'
              Url = 'GROUNDER_URL'; Family = 'GROUNDER_FAMILY'; DescribeUrl = 'DESCRIBE_URL' }
$renamed = @{}
Get-ChildItem Env: | Where-Object Name -like 'CONDUCTOR_MCP_*' | ForEach-Object {
  $new = 'CONDUCTOR_DESKTOP_' + $_.Name.Substring('CONDUCTOR_MCP_'.Length)
  if (-not [Environment]::GetEnvironmentVariable($new)) { Set-Item "Env:$new" $_.Value; $renamed[$new] = $true }
}
foreach ($p in $fromEnv.Keys) {
  $name = "CONDUCTOR_DESKTOP_$($fromEnv[$p])"
  if ($renamed[$name] -and -not $PSBoundParameters.ContainsKey($p)) { Set-Variable $p ([Environment]::GetEnvironmentVariable($name)) }
}
# Installs made before the rename live under the old folder; keep updating it.
$oldDir = Join-Path $env:LOCALAPPDATA 'conductor-mcp'
if (-not $PSBoundParameters.ContainsKey('Dir') -and -not $env:CONDUCTOR_DESKTOP_HOME -and
    -not (Test-Path $Dir) -and (Test-Path $oldDir)) { $Dir = $oldDir }
$Repo = if ($env:CONDUCTOR_DESKTOP_REPO) { $env:CONDUCTOR_DESKTOP_REPO } else { 'yuuko-eth/conductor-desktop' }
$ServerName = 'conductor-desktop'

function Say($m)  { Write-Host ">> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host ">> warning: $m" -ForegroundColor Yellow }

# Prompts only when someone can answer: not with -Yes, not without a console.
$CanAsk = (-not $Yes) -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
function Ask($prompt, $default) {
  if (-not $CanAsk) { return $default }
  $a = Read-Host $prompt
  if ($a) { $a } else { $default }
}

if (-not [Environment]::Is64BitOperatingSystem) { throw 'conductor-desktop needs 64-bit Windows.' }

# -- 1. uv ------------------------------------------------------------------
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
  Say 'installing uv (astral.sh)'
  Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null
  $env:Path = "$env:USERPROFILE\.local\bin;" + [Environment]::GetEnvironmentVariable('Path', 'User') + ";$env:Path"
  if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv did not install; see https://docs.astral.sh/uv/' }
}

# -- 2. source --------------------------------------------------------------
# Updating replaces everything but .venv, so a rerun keeps the environment.
function Replace-Tree($from) {
  New-Item -ItemType Directory -Force -Path $Dir | Out-Null
  Get-ChildItem -Force $Dir | Where-Object { $_.Name -ne '.venv' } | Remove-Item -Recurse -Force
  # A source folder may be someone's checkout: never copy its .venv or .git.
  Get-ChildItem -Force $from | Where-Object { $_.Name -notin '.venv', '.git' } | Copy-Item -Destination $Dir -Recurse -Force
}
function Expand-Source($archive, $into) {
  New-Item -ItemType Directory -Force -Path $into | Out-Null
  if ($archive -like '*.zip') { Expand-Archive -Path $archive -DestinationPath $into -Force }
  else { tar -xzf $archive -C $into; if ($LASTEXITCODE) { throw "couldn't unpack $archive" } }
  # A GitHub archive has one top folder; a plain archive of the tree doesn't.
  if (Test-Path (Join-Path $into 'pyproject.toml')) { return $into }
  (Get-ChildItem -Directory $into | Select-Object -First 1).FullName
}

Say "conductor-desktop -> $Dir"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("conductor-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
  if ($Source -and (Test-Path $Source -PathType Container)) {
    Replace-Tree (Resolve-Path $Source).Path
  } else {
    if ($Source -match '^https?://') {
      $ext = if ($Source -like '*.zip') { 'zip' } else { 'tar.gz' }
      $archive = Join-Path $tmp "src.$ext"
      Invoke-WebRequest -Uri $Source -OutFile $archive -UseBasicParsing
    } elseif ($Source) {
      $archive = (Resolve-Path $Source).Path
    } else {
      $archive = Join-Path $tmp 'src.zip'
      Invoke-WebRequest -Uri "https://codeload.github.com/$Repo/zip/$Ref" -OutFile $archive -UseBasicParsing
    }
    # Files downloaded from the internet carry Mark-of-the-Web, which follows
    # them out of the archive and makes SmartScreen block the venv's .exe.
    Unblock-File -Path $archive -ErrorAction SilentlyContinue
    Replace-Tree (Expand-Source $archive (Join-Path $tmp 'src'))
  }
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
if (-not (Test-Path (Join-Path $Dir 'pyproject.toml'))) { throw "no pyproject.toml in $Dir; the source looks wrong" }

# -- 3. deps and .venv --------------------------------------------------------
& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Dir 'setup\setup-deps-windows.ps1')
if ($LASTEXITCODE) { throw 'the setup script failed (see above)' }
$Cmd = Join-Path $Dir '.venv\Scripts\conductor-desktop.exe'
if (-not (Test-Path $Cmd)) { throw "$Cmd is missing; the sync didn't finish" }

# -- 4. check -----------------------------------------------------------------
# Over SSH Windows puts you in session 0, which has no desktop to capture.
if (-not $NoCheck) {
  if ((Get-Process -Id $PID).SessionId -eq 0) {
    Say 'running in session 0 (SSH or a service), so skipping the check. Run it at the desktop:'
    Write-Host "     $(Join-Path $Dir '.venv\Scripts\conductor-desktop-check.exe')"
  } else {
    Say 'checking capture and input'
    & (Join-Path $Dir '.venv\Scripts\conductor-desktop-check.exe')
    if ($LASTEXITCODE) { Warn 'the check failed; see above, and the Windows guide' }
  }
}

# -- 5. grounder ----------------------------------------------------------------
if (-not $Grounder) {
  if ($Token -or $Family -eq 'managed') { $Grounder = 'managed' }
  elseif ($Url) { $Grounder = 'self-host' }
  else {
    if ($CanAsk) {
      Write-Host ''
      Write-Host 'A grounder model lets the agent find things by description and read the'
      Write-Host 'screen as text. Without one it works from screenshots.'
    }
    $Grounder = switch (Ask 'Grounder: [1] none (default)  [2] managed gateway  [3] my own model server' '1') {
      { $_ -in '2', 'managed' } { 'managed' }
      { $_ -in '3', 'self-host' } { 'self-host' }
      default { 'none' }
    }
  }
}
$genv = [ordered]@{}
switch ($Grounder) {
  'managed' {
    if (-not $Gateway) { $Gateway = if ($Url) { $Url } else { Ask 'Gateway URL' '' } }
    if (-not $Gateway) { throw 'managed needs -Gateway <url>' }
    if (-not $Token) { $Token = Ask 'Seat token (cgr_live_...)' '' }
    if (-not $Token) { throw 'managed needs -Token <cgr_live_...>' }
    if ($Token -notlike 'cgr_live_*') { Warn "that doesn't look like a seat token (cgr_live_...)" }
    $genv['CONDUCTOR_DESKTOP_GROUNDER_URL'] = $Gateway
    $genv['CONDUCTOR_DESKTOP_GROUNDER_FAMILY'] = 'managed'
    $genv['CONDUCTOR_DESKTOP_API_KEY'] = $Token
  }
  'self-host' {
    if (-not $Url) { $Url = Ask 'Model server URL [http://127.0.0.1:8080]' 'http://127.0.0.1:8080' }
    if (-not $Family) { $Family = Ask 'Model family [qwen]' 'qwen' }
    $genv['CONDUCTOR_DESKTOP_GROUNDER_URL'] = $Url
    if ($Family -ne 'qwen') { $genv['CONDUCTOR_DESKTOP_GROUNDER_FAMILY'] = $Family }
    # Point-only and grounding-only families need a describe backend.
    if ($Family -in 'holo', 'locateanything', 'venus', 'guiowl', 'maiui', 'evocua', 'uitars') {
      if (-not $DescribeUrl) { $DescribeUrl = Ask "'$Family' can't describe. Describe server URL (Qwen3-VL), or Enter to skip" '' }
      if ($DescribeUrl) {
        $genv['CONDUCTOR_DESKTOP_DESCRIBE_URL'] = $DescribeUrl
        $genv['CONDUCTOR_DESKTOP_DESCRIBE_FAMILY'] = if ($env:CONDUCTOR_DESKTOP_DESCRIBE_FAMILY) { $env:CONDUCTOR_DESKTOP_DESCRIBE_FAMILY } else { 'qwen' }
      } else { Warn "no describe backend: read_screen and crop won't work with '$Family' alone" }
    }
  }
}

# -- 6. MCP clients ---------------------------------------------------------------
# Claude Desktop from the Microsoft Store keeps its config under its package.
$storeCfg = Get-ChildItem "$env:LOCALAPPDATA\Packages" -Directory -Filter 'Claude_*' -ErrorAction SilentlyContinue |
  Select-Object -First 1 | ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude\claude_desktop_config.json' }
$cdCfg = if ($storeCfg) { $storeCfg } else { Join-Path $env:APPDATA 'Claude\claude_desktop_config.json' }
$piCfg = Join-Path $env:USERPROFILE '.pi\agent\mcp.json'

if (-not $Client) {
  $found = @()
  if (Get-Command claude -ErrorAction SilentlyContinue) { $found += 'claude' }
  if ($storeCfg -or (Test-Path (Split-Path $cdCfg))) { $found += 'claude-desktop' }
  if ((Get-Command pi -ErrorAction SilentlyContinue) -or (Test-Path (Split-Path $piCfg))) { $found += 'pi' }
  $Client = if ($found) { $found -join ',' } else { 'none' }
  if ($Client -ne 'none') { $Client = Ask "Register with [$Client]? (or a list, or none)" $Client }
}

# Merge the server into a JSON config, keeping everything else in it.
function Merge-Json($path) {
  New-Item -ItemType Directory -Force -Path (Split-Path $path) | Out-Null
  if (Test-Path $path) { Copy-Item $path "$path.bak.$([DateTimeOffset]::Now.ToUnixTimeSeconds())" }
  $env:CFG = $path; $env:SVR = $ServerName; $env:CMD_PATH = $Cmd
  $env:GENV = ($genv.Keys | ForEach-Object { "$_=$($genv[$_])" }) -join "`n"
  @'
import json, os
p = os.environ["CFG"]
try:
    data = json.load(open(p, encoding="utf-8-sig")) if os.path.exists(p) else {}
except Exception:
    data = {}  # unreadable: the backup was taken above
env = dict(l.split("=", 1) for l in os.environ["GENV"].splitlines() if "=" in l)
entry = {"command": os.environ["CMD_PATH"], "args": []}
if env:
    entry["env"] = env
data.setdefault("mcpServers", {})[os.environ["SVR"]] = entry
with open(p, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
'@ | & (Join-Path $Dir '.venv\Scripts\python.exe') -
  if ($LASTEXITCODE) { throw "couldn't update $path" }
}

foreach ($c in ($Client -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })) {
  switch ($c) {
    'none' { }
    'claude' {
      if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Warn "claude isn't installed; skipping Claude Code"; continue }
      $eargs = @(); foreach ($k in $genv.Keys) { $eargs += @('-e', "$k=$($genv[$k])") }
      # --scope user: the default (local) scope only applies in this folder.
      # The name goes before -e: -e takes a list and would swallow it.
      & claude mcp remove --scope user $ServerName 2>$null | Out-Null
      & claude mcp add --scope user $ServerName @eargs -- $Cmd | Out-Null
      Say "Claude Code: added '$ServerName' (user scope). Restart it, or /mcp to connect."
    }
    'claude-desktop' { Merge-Json $cdCfg; Say "Claude Desktop: added '$ServerName' to $cdCfg. Quit and reopen it." }
    'pi' { Merge-Json $piCfg; Say "pi: added '$ServerName' to $piCfg" }
    default { Warn "unknown client '$c' (claude, claude-desktop, pi, none)" }
  }
}

Write-Host ''
Say "done. conductor-desktop is in $Dir"
if ($Client -eq 'none') {
  Write-Host "   To use it, point your MCP client at: $Cmd"
  foreach ($k in $genv.Keys) { Write-Host "   with env: $k=$($genv[$k])" }
}
Write-Host '   To drive elevated apps (Task Manager, admin PowerShell), run your client as administrator.'
Write-Host "   Update: rerun this installer. Docs: https://github.com/$Repo"
