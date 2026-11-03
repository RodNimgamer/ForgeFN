@echo off
title Building ForgeFN 2.0
cd /d "%~dp0"

echo [1/3] Checking dependencies...
python -m pip install customtkinter psutil pyinstaller

echo.
echo [2/3] Wiping old build files...
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
del /q *.spec 2>nul

echo.
echo [3/3] Compiling ForgeFN.exe (this takes 30-60 seconds)...
python -m PyInstaller --noconsole --onefile --uac-admin --collect-all customtkinter --name "ForgeFN" --clean forgefn.py

echo.
if exist "dist\ForgeFN.exe" (
    echo ========================================
    echo SUCCESS! Executable is in the dist folder.
    echo ========================================
) else (
    echo ========================================
    echo FAILED! See the error message above.
    echo ========================================
)

echo.
pause
