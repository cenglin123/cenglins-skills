@echo off
rem foxgrep launcher - uses bundled Python runtime, fully decoupled from system env.
rem If runtime is missing (e.g. deployed from source repo), bootstrap-download it first.
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0runtime\python.exe" (
  echo runtime not found, bootstrapping embedded Python...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-runtime.ps1"
  if errorlevel 1 exit /b 1
)
"%~dp0runtime\python.exe" "%~dp0foxgrep.py" %*
