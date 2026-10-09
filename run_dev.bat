@echo off
setlocal
cls
set "dirpath=%~dp0"
if "%dirpath:~-1%" == "\" set "dirpath=%dirpath:~0,-1%"
set /p "choice=Start with a console? (y/n) "
for /f "delims=" %%I in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do set "python=%%I"
if not defined python (
    echo Could not find the system Python 3 installation.
    pause
    exit /b 1
)
if /I "%choice%"=="y" (
    set "exepath=%python%"
) else (
    set "exepath=%python:python.exe=pythonw.exe%"
)
if not exist "%exepath%" (
    echo Python executable not found: "%exepath%"
    pause
    exit /b 1
)
cd /d "%dirpath%"
start "TwitchDropsMiner" "%exepath%" "%dirpath%\main.py"
