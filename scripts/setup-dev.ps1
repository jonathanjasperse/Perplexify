#requires -Version 5.1
<#
.SYNOPSIS
    Creates the .venv development environment (Python 3.12) and installs the app's dependencies, at the same
    versions the installer bundles (installer\windows-constraints.txt), then runs the unit tests.

    In VS Code: Terminal > Run Task > "Set up dev environment".
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root
$venvPython = Join-Path $Root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) { throw 'The Python launcher (py.exe) was not found. Install Python 3.12 (64-bit) from https://www.python.org/downloads/windows/ and run this again.' }
    Write-Host 'Creating .venv with Python 3.12 ...'
    & py.exe -3.12 -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the virtual environment: is Python 3.12 (64-bit) installed? "py -3.12 --version" should work.' }
}

Write-Host 'Installing dependencies ...'
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip could not update itself (is there an internet connection?)' }
& $venvPython -m pip install -r requirements.txt -c installer\windows-constraints.txt
if ($LASTEXITCODE -ne 0) { throw 'pip could not install the dependencies' }

Write-Host ''
Write-Host 'Running the unit tests ...'
& $venvPython -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) { throw 'Some tests failed.' }

Write-Host ''
Write-Host 'Development environment ready. Press F5 in VS Code to run the app, or Ctrl+Shift+B to build the installer.' -ForegroundColor Green
