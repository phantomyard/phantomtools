@echo off
rem phantomforge.cmd — run PhantomForge from the repo checkout (Windows).
rem
rem Manual install (no installer on Windows yet): add this repo's bin
rem directory to your PATH, e.g.
rem   setx PATH "%PATH%;C:\path\to\phantomforge\bin"
rem or set it via System Properties > Environment Variables.
rem Requirements: Python 3.10+ on PATH (or a repo .venv).
rem
rem This wrapper resolves the repo root from its own location, so it must
rem stay inside the repo layout (bin\phantomforge.cmd).
setlocal
set "HERE=%~dp0"
for %%I in ("%HERE%..") do set "REPO=%%~fI"
if exist "%REPO%\.venv\Scripts\python.exe" (
    set "PY=%REPO%\.venv\Scripts\python.exe"
) else (
    set "PY=python"
)
set "PYTHONPATH=%REPO%"
"%PY%" -m phantomforge.cli %*
exit /b %ERRORLEVEL%
