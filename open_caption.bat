@echo off
REM Open the caption mini window as a standalone Chrome app window
REM Usage: open_caption.bat [width] [height]
REM Keep the server (run_live.bat) running before using this.
setlocal
set W=420
set H=260
if not "%~1"=="" set W=%~1
if not "%~2"=="" set H=%~2

set CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=C:\Program Files (x86)\Google\Chrome/Application/chrome.exe
if not exist "%CHROME%" (
  echo [ERROR] Chrome not found. Open http://127.0.0.1:8850/?mini=1 manually.
  pause
  exit /b 1
)
start "" "%CHROME%" --app=http://127.0.0.1:8850/?mini=1 --window-size=%W%,%H% --window-position=30,30
endlocal
