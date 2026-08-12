@echo off
:: Restart KiroCrew Gateway — kills all existing instances then starts fresh.

echo Stopping gateway...

:: Kill the gateway python processes
wmic process where "CommandLine like '%%-m kiro_crew gateway%%'" delete >nul 2>&1

:: Kill all wrapper bat/cmd loops
wmic process where "CommandLine like '%%run-gateway%%'" delete >nul 2>&1

:: Kill kiro-cli sessions
taskkill /IM kirocrew.exe /F >nul 2>&1

:: Remove stale PID file
del "%USERPROFILE%\.kiro\crew\run\gateway-5476.pid" >nul 2>&1

echo Waiting for cleanup...
timeout /t 3 /nobreak >nul

:: Start the gateway directly (same as what the scheduled task does)
echo Starting gateway...
wscript.exe "D:\MyProjects\KiroCrew\scripts\run-gateway-hidden.vbs"
echo Done.
