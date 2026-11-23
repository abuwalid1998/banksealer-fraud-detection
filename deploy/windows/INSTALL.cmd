@echo off
REM VeriShield offline installer - double-click to run (asks for Administrator rights).
REM Options can be passed, e.g.:  INSTALL.cmd -Mode Pilot -Port 9443
cd /d "%~dp0"
net session >nul 2>&1
if %errorlevel% neq 0 (
  if "%~1"=="" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  ) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  )
  exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-VeriShield.ps1" %*
echo.
pause
