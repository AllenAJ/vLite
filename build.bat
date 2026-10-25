@echo off
rem Builds a standalone dist\VLite.exe (run once; needs Python 3.9+ from python.org)
cd /d "%~dp0"
where py >nul 2>nul || (echo Python 3 not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
py -m pip install --upgrade pyinstaller || goto :fail
py -m PyInstaller --onefile --noconsole --name VLite --clean vlite.py || goto :fail
echo.
echo Done: %~dp0dist\VLite.exe  (copy it anywhere, e.g. your Desktop)
pause
exit /b 0
:fail
echo Build failed - see the messages above.
pause
exit /b 1
