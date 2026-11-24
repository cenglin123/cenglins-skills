@echo off
rem foxgrep launcher - order: bundled runtime\python.exe -> system Python 3.8+ (py -3, then python) -> bootstrap-download embedded Python.
rem System Python comes before downloading so offline machines that already have Python 3 work without network access.
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" "%~dp0foxgrep.py" %*
  exit /b
)
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 (
  py -3 "%~dp0foxgrep.py" %*
  exit /b
)
rem python on PATH; the Microsoft Store alias stub fails this check and is skipped
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if not errorlevel 1 (
  python "%~dp0foxgrep.py" %*
  exit /b
)
echo runtime not found and no system Python 3.8+, bootstrapping embedded Python...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-runtime.ps1"
if errorlevel 1 exit /b 1
"%~dp0runtime\python.exe" "%~dp0foxgrep.py" %*
