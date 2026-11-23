@echo off
REM One-command local start on Windows: creates .venv, installs dependencies, runs http://localhost:8000
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment...
  py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
python -m pip install --quiet -r requirements-verishield.txt
python run_verishield.py %*
