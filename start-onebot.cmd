@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-onebot.ps1" %*
set "ONEBOT_EXIT=%ERRORLEVEL%"
pause
exit /b %ONEBOT_EXIT%
