@echo off
rem phantombridge - Jitsi <-> Nostr presence bridge for phantombot personas.
rem Windows launcher: runs bridge.js from the checkout with node.
setlocal
set DIR=%~dp0..
if not exist "%DIR%\bridge.js" (
  echo error: bridge.js not found next to launcher (%DIR%)
  exit /b 1
)
node "%DIR%\bridge.js" %*
exit /b %errorlevel%
