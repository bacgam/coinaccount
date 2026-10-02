@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_tax_tool.ps1"
if errorlevel 1 (
  echo.
  echo [CoinAccount] Setup failed. Review the message above.
  pause
  exit /b 1
)
echo.
echo [CoinAccount] Setup completed. Run start_tax_tool.cmd.
endlocal
