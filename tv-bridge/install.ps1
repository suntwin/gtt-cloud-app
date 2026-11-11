# One-time setup: start everything at login, and add a "TradingView (debug)" shortcut to the Desktop and Start menu.
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$sh = New-Object -ComObject WScript.Shell
$icon = (Get-AppxPackage -Name '*TradingView*' -ErrorAction SilentlyContinue | Select-Object -First 1).InstallLocation
$icon = if ($icon) { Join-Path $icon 'TradingView.exe' } else { "$env:LOCALAPPDATA\Programs\TradingView\TradingView.exe" }

$s = $sh.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'SwingDesk TradingView bridge.lnk'))
$s.TargetPath = 'wscript.exe'; $s.Arguments = "`"$dir\start-all.vbs`""; $s.WorkingDirectory = $dir; $s.Save()

foreach ($where in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {
  $s = $sh.CreateShortcut((Join-Path $where 'TradingView (debug).lnk'))
  $s.TargetPath = 'powershell.exe'
  $s.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$dir\start-tradingview.ps1`""
  $s.WorkingDirectory = $dir; $s.WindowStyle = 7
  if (Test-Path $icon) { $s.IconLocation = "$icon,0" }
  $s.Save()
}
# swingdesk-tv:// link -> lets the scanner start the helper (and TradingView) when you click a ticker and it isn't running
$k = 'HKCU:\Software\Classes\swingdesk-tv'
New-Item -Path "$k\shell\open\command" -Force | Out-Null
Set-ItemProperty -Path $k -Name '(Default)' -Value 'URL:SwingDesk TradingView helper'
Set-ItemProperty -Path $k -Name 'URL Protocol' -Value ''
Set-ItemProperty -Path "$k\shell\open\command" -Name '(Default)' -Value "wscript.exe `"$dir\start-all.vbs`""

"Done. Startup entry and 'TradingView (debug)' shortcuts and the swingdesk-tv:// link created. Starting now..."
Start-Process wscript.exe "`"$dir\start-all.vbs`""
