@echo off
REM Open the caption mini window as a standalone Chrome app window
REM Default size: 1/2 screen width x 1/3 screen height
REM Usage: open_caption.bat [width] [height]   (explicit args override defaults)
REM Keep the server (run_live.bat) running before using this.
setlocal

REM Get primary screen resolution
set SW=1920
set SH=1080
for /f "tokens=1,2 delims=," %%a in ('powershell -NoProfile -Command "Add-Type -AssemblyName System.Windows.Forms; '{0},{1}' -f [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Width, [System.Windows.Forms.Screen]::PrimaryScreen.Bounds.Height"') do (
  set SW=%%a
  set SH=%%b
)
set /a W=SW/2
set /a H=SH/3

REM Explicit args override
if not "%~1"=="" set W=%~1
if not "%~2"=="" set H=%~2

set CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=C:\Program Files (x86)\Google\Chrome/Application/chrome.exe
if not exist "%CHROME%" (
  echo [ERROR] Chrome not found. Open http://127.0.0.1:8850/?mini=1 manually.
  pause
  exit /b 1
)
start "" "%CHROME%" --app=http://127.0.0.1:8850/?mini=1 --window-size=%W%,%H% --window-position=30,30 --user-data-dir="%LOCALAPPDATA%\live-jp-zh-chrome"
endlocal
