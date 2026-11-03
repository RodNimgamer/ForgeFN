@echo off
title Building ForgeFN Executable
echo [1/3] Checking dependencies...
pip install -r requirements.txt

echo [2/3] Building standalone executable with PyInstaller...
pyinstaller --noconsole --onefile --uac-admin --name "ForgeFN" --clean forgefn.py

echo.
echo [3/3] Build Complete!
echo You can find ForgeFN.exe inside the "dist" folder.
echo.
pause
