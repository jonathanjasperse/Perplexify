#requires -Version 5.1
<#
.SYNOPSIS
    Builds the Perplexify installer for Windows:  dist\Perplexify-Setup-<version>-x64.exe

.DESCRIPTION
    1. runs the unit tests
    2. stages a self-contained copy of the app in installer\build\stage:
         the pinned embeddable Python, every package in requirements.txt, the app itself,
         the built-in embedding model, the icon and the licence notices
    3. runs the self-test against that staged copy, offline
    4. compiles it into one installer with Inno Setup

    Downloads are cached in installer\cache, so later builds are much faster.
    In VS Code: Terminal > Run Build Task (Ctrl+Shift+B).

.PARAMETER Fast
    Quick compression, for test builds (the installer comes out larger).
.PARAMETER Clean
    Throw away installer\build first and stage everything again.
.PARAMETER SkipTests
    Do not run the unit tests.
.PARAMETER SkipSelfTest
    Do not run the self-test on the staged copy.
.PARAMETER Relock
    Rewrite installer\windows-constraints.txt from the packages installed in the dev environment first.
.PARAMETER SignCommand
    Optional code-signing command, with $f standing for the file, for example:
    'signtool sign /fd sha256 /td sha256 /tr http://timestamp.digicert.com /a $f'
#>
[CmdletBinding()]
param(
    [switch]$Fast,
    [switch]$Clean,
    [switch]$SkipTests,
    [switch]$SkipSelfTest,
    [switch]$Relock,
    [string]$SignCommand = ''
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is far faster without its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$Here  = $PSScriptRoot
$Root  = Split-Path -Parent $Here
$Cache = Join-Path $Here 'cache'
$Build = Join-Path $Here 'build'
$Stage = Join-Path $Build 'stage'
$Dist  = Join-Path $Root 'dist'
$Clock = [Diagnostics.Stopwatch]::StartNew()

function Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }
function Info([string]$Text) { Write-Host "    $Text" }

# Runs a program and stops the build if it fails. (Do not redirect 2>&1 here: Windows PowerShell 5.1 turns
# every line a program writes to stderr into an error record.)
function Run {
    param([string]$Exe, [string[]]$Arguments = @())
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path -Leaf $Exe) stopped with exit code $LASTEXITCODE" }
}

function RunText {
    param([string]$Exe, [string[]]$Arguments = @())
    $out = & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$(Split-Path -Leaf $Exe) stopped with exit code $LASTEXITCODE" }
    return (($out | Out-String).Trim())
}

function Get-TextHash([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '').ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Get-FileSha256([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

function Get-Download([string]$Url, [string]$File) {
    if (Test-Path -LiteralPath $File) { return }
    Info "downloading $Url"
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $File) | Out-Null
    $part = "$File.part"
    Invoke-WebRequest -Uri $Url -OutFile $part -UseBasicParsing
    Move-Item -LiteralPath $part -Destination $File -Force
}

function Assert-Signed([string]$File, [string]$Signer) {
    $sig = Get-AuthenticodeSignature -LiteralPath $File
    if ($sig.Status -ne 'Valid') { throw "$File does not carry a valid digital signature (status: $($sig.Status))." }
    if ($sig.SignerCertificate.Subject -notlike "*$Signer*") { throw "$File is signed by '$($sig.SignerCertificate.Subject)', not by '$Signer'." }
    Info "signature ok ($Signer): $(Split-Path -Leaf $File)"
}

# robocopy returns 0-7 for success
function Mirror([string]$From, [string]$To, [string[]]$Extra = @()) {
    New-Item -ItemType Directory -Force -Path $To | Out-Null
    & robocopy.exe $From $To /MIR /NFL /NDL /NJH /NJS /NP /R:2 /W:1 @Extra | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $From failed (robocopy exit code $LASTEXITCODE)." }
    $global:LASTEXITCODE = 0
}

function Remove-Folders([string]$Base, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Base)) { return }
    $found = @(Get-ChildItem -LiteralPath $Base -Recurse -Directory -Force -Filter $Name -ErrorAction SilentlyContinue)
    foreach ($d in ($found | Sort-Object { $_.FullName.Length } -Descending)) {
        if (Test-Path -LiteralPath $d.FullName) { Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
    }
}

function Find-Iscc {
    $candidates = @($env:ISCC)
    $onPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")
    foreach ($key in 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1',
                     'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1') {
        $p = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
        if ($p -and $p.PSObject.Properties['InstallLocation']) { $candidates += (Join-Path $p.InstallLocation 'ISCC.exe') }
    }
    foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c)) { return $c } }
    throw "Inno Setup 6 was not found. Install it with:  winget install JRSoftware.InnoSetup   (or from https://jrsoftware.org/isdl.php) and run the build again."
}

function Find-BuildPython([string]$Minor) {
    $notes = @()
    $venv = Join-Path $Root '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) {
        $v = RunText $venv @('-c', "import sys; print('%d.%d' % sys.version_info[:2])")
        if ($v -eq $Minor) { return @{ Exe = $venv; Dev = $true } }
        $notes += ".venv is Python $v"
    }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $exe = & $py.Source "-$Minor" -c "import sys; print(sys.executable)"
        if ($LASTEXITCODE -eq 0 -and $exe) { return @{ Exe = (($exe | Out-String).Trim()); Dev = $false } }
        $global:LASTEXITCODE = 0
    }
    throw "Python $Minor (64-bit) is needed to build the installer; it downloads the packages. Install it from python.org, then run the 'Set up dev environment' task. $($notes -join '; ')"
}

try {
    # ------------------------------------------------------------------ what we are building
    $Pins = Get-Content -LiteralPath (Join-Path $Here 'pins.json') -Raw | ConvertFrom-Json
    $initText = Get-Content -LiteralPath (Join-Path $Root 'perplexify\__init__.py') -Raw
    if ($initText -notmatch '__version__\s*=\s*"(\d+\.\d+\.\d+)"') { throw 'perplexify\__init__.py must contain  __version__ = "x.y.z"' }
    $Version  = $Matches[1]
    $Version4 = "$Version.0"
    $PyMinor  = ($Pins.python.version -split '\.')[0..1] -join '.'
    $Publisher = $Pins.app.publisher
    $Output   = Join-Path $Dist "Perplexify-Setup-$Version-x64.exe"
    $Requirements = Join-Path $Root 'requirements.txt'
    $Constraints  = Join-Path $Here 'windows-constraints.txt'
    $SourceOnly   = Join-Path $Here 'source-only-requirements.txt'
    Write-Host "Perplexify $Version  -  Windows installer build" -ForegroundColor White

    Step 'Tools'
    $Iscc = Find-Iscc
    $Py   = Find-BuildPython $PyMinor
    $BuildPy = $Py.Exe
    Info "Inno Setup  : $Iscc"
    Info "Build Python: $BuildPy ($(RunText $BuildPy @('--version')))"

    if ($Clean -and (Test-Path -LiteralPath $Build)) { Step 'Cleaning installer\build'; Remove-Item -LiteralPath $Build -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $Cache, $Build, $Stage, $Dist | Out-Null

    if ($Relock) {
        Step 'Rewriting windows-constraints.txt from the dev environment'
        $header = @('# The exact package versions the Windows installer bundles: the set the unit tests pass with.',
                    '# Regenerate from the dev environment with:  installer\build.ps1 -Relock   (or: pip freeze > this file)',
                    '# The build installs requirements.txt with these as constraints, so installer builds are repeatable.')
        $frozen = @(& $BuildPy -m pip freeze)
        if ($LASTEXITCODE -ne 0) { throw 'pip freeze failed' }
        Set-Content -LiteralPath $Constraints -Value ($header + $frozen) -Encoding ASCII
        Info "$($frozen.Count) packages written"
    }

    # ------------------------------------------------------------------ tests
    if (-not $SkipTests) {
        Step 'Unit tests'
        if ($Py.Dev) {
            Push-Location $Root
            try { Run $BuildPy @('-m', 'unittest', 'discover', '-s', 'tests', '-q') } finally { Pop-Location }
        } else { Info 'skipped: no .venv with the app dependencies (run the "Set up dev environment" task)' }
    }

    # ------------------------------------------------------------------ runtime: embeddable Python + packages
    Step "Python $($Pins.python.version) runtime and packages"
    $Runtime = Join-Path $Stage 'runtime'
    $Site    = Join-Path $Runtime 'Lib\site-packages'
    $Marker  = Join-Path $Runtime '.deps-key'
    $depsKey = Get-TextHash ((Get-Content -LiteralPath $Requirements -Raw) + (Get-Content -LiteralPath $Constraints -Raw) +
                             (Get-Content -LiteralPath $SourceOnly -Raw) + $Pins.python.version + $Pins.python.sha256)
    if ((Test-Path -LiteralPath $Marker) -and ((Get-Content -LiteralPath $Marker -Raw).Trim() -eq $depsKey)) {
        Info 'up to date (requirements and Python version unchanged)'
    } else {
        if (Test-Path -LiteralPath $Runtime) { Remove-Item -LiteralPath $Runtime -Recurse -Force }
        $zip = Join-Path $Cache (Split-Path -Leaf $Pins.python.url)
        Get-Download $Pins.python.url $zip
        $hash = Get-FileSha256 $zip
        if ($hash -ne $Pins.python.sha256.ToLowerInvariant()) {
            Remove-Item -LiteralPath $zip -Force
            throw "The downloaded Python zip has SHA-256 $hash but installer\pins.json pins $($Pins.python.sha256). The cached file was deleted."
        }
        Expand-Archive -LiteralPath $zip -DestinationPath $Runtime -Force
        Assert-Signed (Join-Path $Runtime 'python.exe') $Pins.python.signer
        Assert-Signed (Join-Path $Runtime "python$($PyMinor -replace '\.', '').dll") $Pins.python.signer

        # Our own search path: the standard library, this folder, the packages and the app. "import site" turns site-packages on.
        $pthName = "python$($PyMinor -replace '\.', '')._pth"
        $pth = "python$($PyMinor -replace '\.', '').zip`r`n.`r`nLib\site-packages`r`n..\app`r`nimport site`r`n"
        [IO.File]::WriteAllText((Join-Path $Runtime $pthName), $pth, (New-Object Text.UTF8Encoding($false)))

        New-Item -ItemType Directory -Force -Path $Site | Out-Null

        # the few packages PyPI ships only as source (pure Python): built into wheels once, from a hash-checked file
        $Wheels = Join-Path $Cache 'wheels'
        $wanted = @(Get-Content -LiteralPath $SourceOnly | Where-Object { $_ -match '^\s*([A-Za-z0-9_.-]+)==' } | ForEach-Object { ($_ -replace '^\s*([A-Za-z0-9_.-]+)==.*$', '$1').Replace('-', '_').ToLowerInvariant() })
        $have = @(Get-ChildItem -LiteralPath $Wheels -Filter '*.whl' -ErrorAction SilentlyContinue | ForEach-Object { ($_.Name -split '-')[0].Replace('-', '_').ToLowerInvariant() })
        if (@($wanted | Where-Object { $have -notcontains $_ }).Count -gt 0) {
            Info "building wheels for source-only packages ($($wanted -join ', '))"
            New-Item -ItemType Directory -Force -Path $Wheels | Out-Null
            Run $BuildPy @('-m', 'pip', 'wheel', '--disable-pip-version-check', '--no-deps', '--require-hashes', '-r', $SourceOnly, '-w', $Wheels)
        }

        Info 'installing packages (wheels only; this takes a minute the first time)'
        Run $BuildPy @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-warn-script-location', '--no-compile',
                       '--only-binary=:all:', '--platform', 'win_amd64', '--python-version', $PyMinor,
                       '--find-links', $Wheels,
                       '--target', $Site, '-r', $Requirements, '-c', $Constraints)

        # what the app never needs at run time
        Remove-Item -LiteralPath (Join-Path $Site 'bin') -Recurse -Force -ErrorAction SilentlyContinue   # console scripts that point at this PC
        Remove-Folders $Site '__pycache__'
        Remove-Folders (Join-Path $Site 'numpy') 'tests'   # numpy's own test suite: large and deeply nested
        Set-Content -LiteralPath $Marker -Value $depsKey -Encoding ASCII
    }
    $RuntimePy = Join-Path $Runtime 'python.exe'

    # ------------------------------------------------------------------ the app itself
    Step 'App files'
    $AppDest = Join-Path $Stage 'app\perplexify'
    # start from nothing: a folder left by an older build (the app was called manualrag once) must not ship
    foreach ($old in @(Get-ChildItem -LiteralPath (Join-Path $Stage 'app') -Directory -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'perplexify' })) {
        Info "removing stale app\$($old.Name)"
        Remove-Item -LiteralPath $old.FullName -Recurse -Force
    }
    Remove-Folders $AppDest '__pycache__'
    Mirror (Join-Path $Root 'perplexify') $AppDest @('/XD', '__pycache__', '/XF', '*.pyc')
    Info "perplexify $Version copied"

    # every program started from here uses a throw-away data folder, never the real %LOCALAPPDATA%\Perplexify
    $env:PERPLEXIFY_HOME = Join-Path $Build 'home'
    New-Item -ItemType Directory -Force -Path $env:PERPLEXIFY_HOME | Out-Null

    # ------------------------------------------------------------------ icon (drawn by perplexify\icon.py; used by the shortcuts and the installer)
    Step 'Icon'
    $Ico = Join-Path $Build 'app.ico'
    Run $RuntimePy @('-P', '-m', 'perplexify.icon', '--ico', $Ico)
    Copy-Item -LiteralPath $Ico -Destination (Join-Path $Stage 'app.ico') -Force

    # ------------------------------------------------------------------ embedding model
    Step "Embedding model $($Pins.embedding_model)"
    $ModelCache = Join-Path $Cache ('models-' + ($Pins.embedding_model -replace '[^A-Za-z0-9.-]', '_'))
    $haveModel = (Test-Path -LiteralPath $ModelCache) -and (@(Get-ChildItem -LiteralPath $ModelCache -Recurse -File -Filter '*.onnx').Count -gt 0)
    if (-not $haveModel) {
        if (Test-Path -LiteralPath $ModelCache) { Remove-Item -LiteralPath $ModelCache -Recurse -Force }
        Info 'downloading (about 65 MB, once)'
        Run $RuntimePy @('-P', '-m', 'perplexify', 'download-model', '--dest', $ModelCache, '--model', $Pins.embedding_model)
    } else { Info 'cached' }
    Mirror $ModelCache (Join-Path $Stage 'models')

    # ------------------------------------------------------------------ licences
    Step 'Licence notices'
    Run $BuildPy @((Join-Path $Here 'scripts\make_notices.py'), $Site, (Join-Path $Stage 'THIRD-PARTY-NOTICES.txt'), $Pins.embedding_model)

    # whatever an older build left in the stage that is no longer part of the product
    $expected = 'runtime', 'app', 'models', 'app.ico', 'THIRD-PARTY-NOTICES.txt'
    foreach ($item in @(Get-ChildItem -LiteralPath $Stage -Force | Where-Object { $expected -notcontains $_.Name })) {
        Info "removing stale $($item.Name)"
        Remove-Item -LiteralPath $item.FullName -Recurse -Force
    }

    # ------------------------------------------------------------------ byte-compile, so the first start is fast and the install folder can be read-only
    Step 'Compiling Python files'
    Run $RuntimePy @('-P', '-m', 'compileall', '-q', '-f', '-j', '0', '--invalidation-mode', 'unchecked-hash', $Site)
    Run $RuntimePy @('-P', '-m', 'compileall', '-q', '-f', '-j', '0', (Join-Path $Stage 'app'))

    $files = @(Get-ChildItem -LiteralPath $Stage -Recurse -File -Force)
    $bytes = ($files | Measure-Object Length -Sum).Sum
    $longest = $files | ForEach-Object { $_.FullName.Substring($Stage.Length) } | Sort-Object Length -Descending | Select-Object -First 1
    Info ("{0:N0} files, {1:N0} MB staged; longest path inside the install folder: {2} characters" -f $files.Count, ($bytes / 1MB), $longest.Length)
    if ($longest.Length -gt 180) { Write-Warning "A path of $($longest.Length) characters ($longest) may exceed Windows' 260-character limit in a deep install folder." }

    # ------------------------------------------------------------------ prove the staged copy works, offline
    if (-not $SkipSelfTest) {
        Step 'Self-test of the staged copy (offline)'
        $env:HF_HUB_OFFLINE = '1'
        try { Run $RuntimePy @('-P', '-X', 'utf8', '-m', 'perplexify', 'selftest') } finally { Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue }
    }

    # ------------------------------------------------------------------ Microsoft Visual C++ runtime
    Step 'Microsoft Visual C++ runtime'
    $VcRedist = Join-Path $Cache 'vc_redist.x64.exe'
    Get-Download $Pins.vc_redist.url $VcRedist     # Microsoft updates this file in place, so it cannot be pinned by hash: its signature is checked instead
    Assert-Signed $VcRedist $Pins.vc_redist.signer
    $vi = (Get-Item -LiteralPath $VcRedist).VersionInfo
    $vcVersion = "$($vi.FileMajorPart).$($vi.FileMinorPart).$($vi.FileBuildPart)"
    Info "bundled version $vcVersion (installed only when the PC has older than $($Pins.vc_redist.min_version))"
    $minParts = $Pins.vc_redist.min_version -split '\.'
    if (([int]$vi.FileMajorPart -lt [int]$minParts[0]) -or (([int]$vi.FileMajorPart -eq [int]$minParts[0]) -and ([int]$vi.FileMinorPart -lt [int]$minParts[1]))) {
        throw "The downloaded Visual C++ runtime ($vcVersion) is older than the minimum in pins.json ($($Pins.vc_redist.min_version)). Delete installer\cache\vc_redist.x64.exe and build again."
    }

    # ------------------------------------------------------------------ Microsoft Edge WebView2 Runtime (what draws the app's window)
    Step 'Microsoft Edge WebView2 Runtime bootstrapper'
    $WebView2 = Join-Path $Cache 'MicrosoftEdgeWebview2Setup.exe'
    Get-Download $Pins.webview2.url $WebView2      # a small stub that Microsoft updates in place: its signature is checked instead of a hash
    Assert-Signed $WebView2 $Pins.webview2.signer
    Info "bundled bootstrapper $((Get-Item -LiteralPath $WebView2).VersionInfo.FileVersion) (run only when the PC has no WebView2 Runtime; Windows 11 and current Windows 10 already have it)"

    # ------------------------------------------------------------------ the installer
    Step 'Compiling the installer with Inno Setup'
    $minVc = $Pins.vc_redist.min_version -split '\.'
    $isccArgs = @("/DAppVersion=$Version", "/DAppVersion4=$Version4", "/DAppPublisher=$Publisher", "/DAppName=$($Pins.app.name)",
                  "/DSourceDir=$Stage", "/DOutputDir=$Dist", "/DIconFile=$Ico", "/DVCRedist=$VcRedist", "/DWebView2Setup=$WebView2",
                  "/DVCMinMajor=$($minVc[0])", "/DVCMinMinor=$($minVc[1])", '/Qp')
    if ($Fast) { $isccArgs += '/DFast' }
    if ($SignCommand) { $isccArgs += "/Ssignit=$SignCommand"; $isccArgs += '/DSignIt' }
    $isccArgs += (Join-Path $Here 'Perplexify.iss')
    Run $Iscc $isccArgs
    if (-not (Test-Path -LiteralPath $Output)) { throw "Inno Setup finished but $Output is missing." }

    $sha = Get-FileSha256 $Output
    Set-Content -LiteralPath "$Output.sha256" -Value "$sha  $(Split-Path -Leaf $Output)" -Encoding ASCII
    Write-Host ''
    Write-Host "Done in $([int]$Clock.Elapsed.TotalSeconds) s" -ForegroundColor Green
    Write-Host "  $Output"
    Write-Host ("  {0:N1} MB   sha256 {1}" -f ((Get-Item -LiteralPath $Output).Length / 1MB), $sha)
    if (-not $SignCommand) { Write-Host '  Not code-signed: Windows SmartScreen will warn about an unknown publisher (see installer\README.md).' -ForegroundColor DarkYellow }
}
catch {
    Write-Host ''
    Write-Host "BUILD FAILED: $($_.Exception.Message)" -ForegroundColor Red
    if ($_.InvocationInfo -and $_.InvocationInfo.ScriptLineNumber) { Write-Host "  at line $($_.InvocationInfo.ScriptLineNumber) of build.ps1" -ForegroundColor DarkRed }
    exit 1
}
finally {
    Remove-Item Env:PERPLEXIFY_HOME -ErrorAction SilentlyContinue
}
