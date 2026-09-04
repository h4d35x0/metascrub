@echo off
setlocal

rem metascrub desktop window. See metascrub.cmd in this directory for why these
rem launchers exist and how they resolve paths.
rem
rem The difference is pythonw: it launches the window with no console attached,
rem which is what pip's `gui-scripts` entry point does for an installed copy.
rem `start ""` returns immediately so the shell is not held open by the window.

set "PROJECT=%~dp0.."
set "TOOLS=%~dp0..\..\..\.tools"

if not exist "%PROJECT%\metascrub\gui.py" (
    echo metascrub-gui: cannot find the package.
    echo   looked in: %PROJECT%\metascrub
    exit /b 1
)

if exist "%TOOLS%\exiftool\exiftool.exe" set "PATH=%TOOLS%\exiftool;%PATH%"
set "PYTHONPATH=%PROJECT%;%PYTHONPATH%"

rem pythonw has no console, so a startup failure would vanish silently. Fall
rem back to python (with a console) when pythonw is absent, so an error is at
rem least visible rather than the window simply never appearing.
where pythonw >nul 2>&1
if %ERRORLEVEL%==0 (
    start "" pythonw -m metascrub gui %*
) else (
    python -m metascrub gui %*
)
exit /b %ERRORLEVEL%
