@echo off
setlocal
set "BASH_PATH="
where bash.exe >nul 2>&1 && set "BASH_PATH=bash.exe"
if not defined BASH_PATH if exist "C:\Program Files\Git\bin\bash.exe" set "BASH_PATH=C:\Program Files\Git\bin\bash.exe"
if not defined BASH_PATH if exist "C:\Program Files (x86)\Git\bin\bash.exe" set "BASH_PATH=C:\Program Files (x86)\Git\bin\bash.exe"
if not defined BASH_PATH if exist "%LOCALAPPDATA%\Programs\Git\bin\bash.exe" set "BASH_PATH=%LOCALAPPDATA%\Programs\Git\bin\bash.exe"

if not defined BASH_PATH (
    echo [ERROR] Git Bash (bash.exe) was not found. Please verify Git for Windows installation. >&2
    exit /b 1
)

"%BASH_PATH%" "%~dp0cnsr.sh" %*
exit /b %ERRORLEVEL%
