' run-gateway-hidden.vbs — Launches KiroCrew gateway with no visible console window.
' Used by the scheduled task in desktop mode.

Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "D:\MyProjects\KiroCrew\scripts"
WshShell.Run "cmd.exe /c run-gateway.bat", 0, False
