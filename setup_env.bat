@echo off
REM Live JP-ZH caption tool - one-time environment setup
REM Creates conda env "live-jp-zh", installs dependencies, downloads models.

setlocal
set CONDA=C:\ProgramData\Anaconda3\Scripts\conda.exe
if not exist "%CONDA%" (
  echo [ERROR] conda.exe not found at %CONDA%
  pause
  exit /b 1
)
set PYDIR=C:\Users\ayore\.conda\envs\live-jp-zh
set PYTHON=%PYDIR%\python.exe
cd /d "%~dp0"

echo === [1/4] Creating conda env live-jp-zh ^(Python 3.11^) ===
if exist "%PYTHON%" (
  echo Env already exists, skipping creation.
) else (
  call "%CONDA%" create -n live-jp-zh python=3.11 -y || goto :fail
)

echo === [2/4] Installing dependencies ===
"%PYTHON%" -m pip install -U pip setuptools wheel || goto :fail
"%PYTHON%" -m pip install -r requirements.txt || goto :fail
REM llama-cpp-python Windows CPU wheel lives on the project's official wheel index
"%PYTHON%" -m pip install llama-cpp-python==0.3.36 --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu || goto :fail

echo === [3/4] Preparing faster-whisper large-v3 model cache ===
if not exist "models\faster-whisper\models--Systran--faster-whisper-large-v3" (
  if exist "D:\claudeFile\20261017-whisper-subs\models\faster-whisper\models--Systran--faster-whisper-large-v3" (
    mklink /J "models\faster-whisper\models--Systran--faster-whisper-large-v3" "D:\claudeFile\20261017-whisper-subs\models\faster-whisper\models--Systran--faster-whisper-large-v3" || goto :fail
    echo Linked model cache from whisper-subs project (no re-download needed^).
  ) else (
    echo whisper-subs cache not found; large-v3 will be downloaded on first run ^(about 3 GB^).
  )
) else (
  echo Model cache already present.
)

echo === [4/4] Downloading Qwen3-4B-Instruct-2507 Q4_K_M GGUF ^(about 2.5 GB^) ===
if exist "models\Qwen3-4B-Instruct-2507-Q4_K_M.gguf" (
  echo GGUF already exists, skipping.
) else (
  "%PYDIR%\Scripts\hf.exe" download unsloth/Qwen3-4B-Instruct-2507-GGUF Qwen3-4B-Instruct-2507-Q4_K_M.gguf --local-dir models || goto :fail
)

"%PYTHON%" -m pip freeze > requirements.lock.txt

echo.
echo Setup finished.
echo Next: double-click run_live.bat, then click the extension icon on the target tab.
pause
exit /b 0

:fail
echo.
echo [ERROR] Setup failed. See messages above.
pause
exit /b 1
