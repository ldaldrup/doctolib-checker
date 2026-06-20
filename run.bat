@echo off
title Doctolib Checker

echo Starting Doctolib Checker...
cd /d "%~dp0"

:: Activate virtual environment if it exists
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
)

python checker.py

:: Pause only if the script crashes so you can see the error output
echo Checker stopped.
pause