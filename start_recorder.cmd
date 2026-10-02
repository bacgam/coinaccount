@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Backpack BTC Paper Data Recorder
".venv\Scripts\python.exe" -m backpack_bot record
if errorlevel 1 pause

