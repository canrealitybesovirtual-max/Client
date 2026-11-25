@echo off
rem TradeComply CA, local mode (Windows). Double-click to run. First run creates .venv and installs requirements;
rem then it starts the app on 127.0.0.1 and opens your browser. Data is kept in %USERPROFILE%\.tradecomply.
setlocal
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up a Python environment in .venv ...
  %PY% -m venv .venv || goto :nopython
  del /q ".venv\.installed" 2>nul
)
if not exist ".venv\.installed" (
  echo Installing requirements, needs internet once ...
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt || goto :fail
  echo installed> ".venv\.installed"
)
".venv\Scripts\python.exe" -m app.local %*
goto :eof

:nopython
echo.
echo Python 3.11 or newer is required. Install it from https://www.python.org/downloads/windows/
echo (tick "Add python.exe to PATH"), then double-click this file again.
pause
exit /b 1

:fail
echo.
echo Installing requirements failed. Check your internet connection and try again.
pause
exit /b 1
