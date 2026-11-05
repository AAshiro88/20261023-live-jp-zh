@echo off
REM Live JP-ZH caption tool launcher
setlocal
cd /d "%~dp0"
REM Python writes UTF-8; switch console codepage so CJK output is readable
chcp 65001 >nul
set "PYTHON=C:\Users\ayore\.conda\envs\live-jp-zh\python.exe"
if not exist "%PYTHON%" (
  echo [ERROR] Env not found. Run setup_env.bat first.
  pause
  exit /b 1
)

REM Chinese interaction is handled by run_live.py; this bat only locates the interpreter
"%PYTHON%" run_live.py %*
REM Keep the window open on failure so the error message stays visible
if errorlevel 1 pause
exit /b %ERRORLEVEL%
