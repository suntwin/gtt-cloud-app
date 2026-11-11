' Runs at login: TradingView in debug mode + tv-bridge, both with no console window.
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "\start-tradingview.ps1""", 0, False
sh.Run "node """ & dir & "\tv-bridge.js""", 0, False
