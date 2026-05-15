@echo off
setlocal
cd /d "%~dp0"

echo Installing Subs tool dependencies ...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Install failed.
  pause
  exit /b 1
)
echo.
echo Done. Start: python app.py
pause
