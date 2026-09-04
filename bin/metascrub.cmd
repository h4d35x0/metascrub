@echo off
setlocal

rem metascrub launcher.
rem
rem This is the real launcher and it lives with the project, so the project
rem directory is self-contained: copy or clone it anywhere and it is runnable,
rem with no install step and nothing to register.
rem
rem It exists because `pip install` cannot produce a launcher that travels. On
rem Windows pip writes a stub into the Python installation's Scripts directory
rem with that interpreter's absolute path baked in (C:\Python314\python.exe),
rem so the launcher is tied to one machine even though the code is not.
rem
rem `<volume>\Projects\.tools\bin\metascrub.cmd` is a two-line forwarder to this
rem file. That directory is on PATH, which is the only reason a bare
rem `metascrub` works from anywhere; it holds no logic.

rem %~dp0 is this script's directory with a trailing backslash:
rem   ...\Metadata-Scrubber\bin\   ->  ..  ->  ...\Metadata-Scrubber\
set "PROJECT=%~dp0.."

rem ...\Metadata-Scrubber\bin\ -> ..\..\.. -> <volume>\Projects\
set "TOOLS=%~dp0..\..\..\.tools"

if not exist "%PROJECT%\metascrub\__main__.py" (
    echo metascrub: cannot find the package.
    echo   looked in: %PROJECT%\metascrub
    echo   This launcher expects to live in the project's bin\ directory.
    exit /b 1
)

rem Prefer an exiftool that travels on the same volume, if there is one.
rem exiftool is required for EVERY format, not just images: all four engines
rem read their baseline metadata through it, because reading with a different
rem tool than the one that wrote is what makes the verification meaningful.
rem Without it metascrub refuses to run at all, so carrying one is what makes
rem the tool portable in practice. A system exiftool is used when the volume
rem has none, which is also what happens if this project is copied out on its
rem own.
if exist "%TOOLS%\exiftool\exiftool.exe" set "PATH=%TOOLS%\exiftool;%PATH%"

rem Run from source. No install step, so the code that runs is always the code
rem sitting next to this launcher.
set "PYTHONPATH=%PROJECT%;%PYTHONPATH%"

where py >nul 2>&1
if %ERRORLEVEL%==0 (
    py -3 -m metascrub %*
) else (
    python -m metascrub %*
)
exit /b %ERRORLEVEL%
