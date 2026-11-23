@echo off
rem Builds the Windows installer:  dist\Perplexify-Setup-<version>-x64.exe
rem Double-click this file, or run it from a terminal. Extra options are passed on, for example:
rem     build-installer.cmd -Fast          quick compression (a larger installer), for test builds
rem     build-installer.cmd -SkipTests     do not run the unit tests first
rem     build-installer.cmd -Clean         stage everything again from nothing
rem In VS Code the same thing is  Terminal > Run Build Task  (Ctrl+Shift+B).
rem Needs Inno Setup 6 (winget install JRSoftware.InnoSetup) and the dev environment (VS Code task "Set up dev environment").
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\build.ps1" %*
set CODE=%ERRORLEVEL%
echo.
if "%CODE%"=="0" (echo Finished.) else (echo The build failed: see the message above.)
if "%1"=="" pause
exit /b %CODE%
