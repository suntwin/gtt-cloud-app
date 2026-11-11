' Starts tv-bridge with no console window. Put a shortcut to this file in shell:startup to run it at login.
Set sh = CreateObject("WScript.Shell")
dir = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
sh.Run "node """ & dir & "\tv-bridge.js""", 0, False
