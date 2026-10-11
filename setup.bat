@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto install
py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto try_python
py -3 -m venv ".venv"
if errorlevel 1 goto failed
goto install

:try_python
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1
if errorlevel 1 goto missing
python -m venv ".venv"
if errorlevel 1 goto failed

:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
echo Setup complete. Use add-folder.bat, then start.bat.
exit /b 0

:missing
echo Install Python 3.10 or newer, then run setup.bat again.
echo https://www.python.org/downloads/windows/
:failed
echo Setup failed. Please check the output above.
pause
exit /b 1
