@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Backpack BTC Historical Candles
".venv\Scripts\python.exe" -m backpack_bot history
if errorlevel 1 pause
