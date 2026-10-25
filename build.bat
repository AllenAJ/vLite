@echo off
rem Builds a standalone dist\VLite.exe (run once; needs Python 3.9+ from python.org)
cd /d "%~dp0"
where py >nul 2>nul || (echo Python 3 not found. Install it from https://www.python.org/downloads/ & pause & exit /b 1)
py -m pip install --upgrade pyinstaller certifi || goto :fail
py -m PyInstaller --onefile --noconsole --name VLite --clean --icon assets\VLite.ico --add-data "assets\VLite.ico;assets" vlite.py || goto :fail
echo.
echo Done: %~dp0dist\VLite.exe  (copy it anywhere, e.g. your Desktop)
pause
exit /b 0
:fail
echo Build failed - see the messages above.
pause
exit /b 1
