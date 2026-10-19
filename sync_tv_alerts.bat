@echo off
rem Sync TradingView alerts to the dashboard (TradingView must be open in debug mode).
cd /d "%~dp0"
"%~dp0venv\Scripts\python.exe" "%~dp0sync_tv_alerts.py" %*
if "%1"=="" pause
