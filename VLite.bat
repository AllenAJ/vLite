@echo off
where pyw >nul 2>nul && (start "" pyw "%~dp0vlite.py" & exit /b)
where pythonw >nul 2>nul && (start "" pythonw "%~dp0vlite.py" & exit /b)
echo Python 3 not found. Install it from https://www.python.org/downloads/ (tick "Add python.exe to PATH").
pause
