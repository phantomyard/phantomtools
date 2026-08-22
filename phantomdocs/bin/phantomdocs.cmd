@echo off
rem phantomdocs.cmd — run PhantomDocs from the repo checkout (Windows).
rem
rem Manual install (no installer on Windows yet): add this repo's bin
rem directory to your PATH, e.g.
rem   setx PATH "%PATH%;C:\path\to\phantomdocs\bin"
rem or set it via System Properties > Environment Variables.
rem Requirements: Python 3.10+ on PATH (or a repo .venv).
rem
setlocal
set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "REPO=%%~fI"
if exist "%REPO%\.venv\Scripts\python.exe" (
    set "PY=%REPO%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)
set "PYTHONPATH=%REPO%"
"%PY%" -m phantomdocs.cli %*
exit /b %ERRORLEVEL%
