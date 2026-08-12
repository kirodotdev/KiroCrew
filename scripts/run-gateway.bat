@echo off
:: KiroCrew Gateway Desktop Mode Wrapper
:: Restarts gateway automatically on crash (5-second delay)
:: Single-instance: exits if gateway port file indicates it's already running.
cd /d "D:\MyProjects\KiroCrew"

:: Check if a gateway is already serving by testing its PID file
set PIDFILE=%USERPROFILE%\.kiro\crew\run\gateway-5476.pid
if exist "%PIDFILE%" (
    for /f %%P in (%PIDFILE%) do (
        tasklist /FI "PID eq %%P" /NH 2>nul | findstr /i "python" >nul
        if not errorlevel 1 (
            echo [%DATE% %TIME%] Gateway already running ^(PID %%P^), exiting wrapper. >> "D:\MyProjects\KiroCrew\gateway-stdout.log"
            exit /b 0
        )
    )
)

:: Set INVOCATION_ID so the built-in /kirocrew restart slash command works
set INVOCATION_ID=scheduled-task

:loop
echo [%DATE% %TIME%] Starting KiroCrew gateway... >> "D:\MyProjects\KiroCrew\gateway-stdout.log"
"D:\MyProjects\KiroCrew\.venv\Scripts\python.exe" -m kiro_crew gateway >> "D:\MyProjects\KiroCrew\gateway-stdout.log" 2>> "D:\MyProjects\KiroCrew\gateway-stderr.log"
echo [%DATE% %TIME%] Gateway exited (code %ERRORLEVEL%), restarting in 5s... >> "D:\MyProjects\KiroCrew\gateway-stdout.log"
timeout /t 5 /nobreak >nul
goto loop
