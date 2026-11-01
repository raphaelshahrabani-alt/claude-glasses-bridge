@echo off
rem Runs glasses.py with whichever Python is installed: glasses start, glasses doctor, ...
where py >nul 2>nul
if %errorlevel%==0 (
  py "%~dp0glasses.py" %*
) else (
  python "%~dp0glasses.py" %*
)
exit /b %errorlevel%
