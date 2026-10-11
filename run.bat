@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  call "%~dp0setup.bat"
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -X utf8 gallery.py %*
exit /b %errorlevel%
