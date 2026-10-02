@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Backpack BTC Paper Monitor
".venv\Scripts\python.exe" -m backpack_bot monitor
if errorlevel 1 pause

