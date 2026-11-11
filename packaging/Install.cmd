@echo off
rem 360 Video Shrinker - per-user install (no admin rights). See "READ ME FIRST.txt".
setlocal
if not exist "%~dp0payload\python\python.exe" (
  echo The package is incomplete: unzip the whole zip file first, then run Install.cmd from the unzipped folder.
  pause
  exit /b 1
)
"%~dp0payload\python\python.exe" "%~dp0payload\setup.py" install %*
if errorlevel 1 (
  echo.
  echo The install did not finish. See the message above.
)
echo.
pause
