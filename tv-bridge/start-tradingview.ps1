# Starts TradingView Desktop with the debug port the scanner button (tv-bridge) and the TradingView MCP use.
#   -NoKill : don't restart a TradingView that is already open without the port (used by tv-bridge)
param([switch]$NoKill)
$port = if ($env:TV_CDP_PORT) { [int]$env:TV_CDP_PORT } else { 9222 }
$arg = "--remote-debugging-port=$port"

function Up { try { Invoke-RestMethod "http://127.0.0.1:$port/json/version" -TimeoutSec 2 | Out-Null; $true } catch { $false } }
function WaitUp($sec) { for ($i = 0; $i -lt $sec; $i++) { if (Up) { return $true }; Start-Sleep 1 }; $false }
function StopTV { Get-Process -Name TradingView -ErrorAction SilentlyContinue | Stop-Process -Force; Start-Sleep 2 }

if (Up) { "TradingView is already in debug mode on port $port."; exit 0 }
if (Get-Process -Name TradingView -ErrorAction SilentlyContinue) {
  if ($NoKill) { "TradingView is open without the debug port. Close it and start it with 'TradingView (debug)'."; exit 2 }
  StopTV
}

$pkg = Get-AppxPackage -Name '*TradingView*' -ErrorAction SilentlyContinue | Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
$candidates = @()
if ($pkg) { $candidates += Join-Path $pkg.InstallLocation 'TradingView.exe' }
$candidates += "$env:LOCALAPPDATA\Programs\TradingView\TradingView.exe", "$env:ProgramFiles\TradingView\TradingView.exe"
$exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $exe) { "TradingView.exe not found."; exit 1 }

Start-Process -FilePath $exe -ArgumentList $arg
if (WaitUp 20) { "Started TradingView in debug mode on port $port."; exit 0 }

# Microsoft Store installs can ignore the debug flag -> run a private copy of the app instead.
# One-time copy (~330 MB, about a minute); copied again only when TradingView updates.
if (-not $pkg) { "TradingView started but port $port did not open."; exit 1 }
StopTV
$copy = Join-Path $env:LOCALAPPDATA 'TradingView-debug'
$stamp = "$copy.version"
if (-not (Test-Path (Join-Path $copy 'TradingView.exe')) -or (Get-Content $stamp -ErrorAction SilentlyContinue) -ne $pkg.Version) {
  "Copying TradingView $($pkg.Version) to $copy (one time)..."
  robocopy $pkg.InstallLocation $copy /MIR /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
  Set-Content $stamp $pkg.Version
}
Start-Process -FilePath (Join-Path $copy 'TradingView.exe') -ArgumentList $arg
if (WaitUp 40) { "Started TradingView (local copy) in debug mode on port $port."; exit 0 }
"Port $port did not open."; exit 1
