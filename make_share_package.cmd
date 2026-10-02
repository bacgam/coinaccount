@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_share_package.ps1"
if errorlevel 1 (
  echo.
  echo [CoinAccount] Failed to create the sharing package.
  pause
  exit /b 1
)
echo.
pause
endlocal
