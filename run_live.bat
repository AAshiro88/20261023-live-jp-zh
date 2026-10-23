@echo off
REM Live JP-ZH caption tool launcher
setlocal
cd /d "%~dp0"
set PYTHON=C:\Users\ayore\.conda\envs\live-jp-zh\python.exe
if not exist "%PYTHON%" (
  echo [ERROR] Env not found. Run setup_env.bat first.
  pause
  exit /b 1
)
"%PYTHON%" live_translate.py %*
pause
