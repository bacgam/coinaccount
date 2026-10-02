@echo off
setlocal
title CoinAccount - Tax Ledger
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if not exist ".venv\Scripts\python.exe" (
  echo [CoinAccount] First-time setup is required.
  call "%~dp0setup_tax_tool.cmd"
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -c "from backpack_tax.app import create_app" >nul 2>nul
if errorlevel 1 (
  echo [CoinAccount] Repairing the private Python environment...
  call "%~dp0setup_tax_tool.cmd"
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m backpack_tax
if errorlevel 1 pause
endlocal
