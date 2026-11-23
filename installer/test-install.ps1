#requires -Version 5.1
<#
.SYNOPSIS
    Tries the finished installer for real: installs it silently into a temporary folder, runs the installed app
    (import, search), upgrades it in place while it is running, then uninstalls it. Leaves nothing behind.

.DESCRIPTION
    Needs no administrator rights. The app's data goes to a temporary folder (PERPLEXIFY_HOME), never to your
    real %LOCALAPPDATA%\Perplexify.

    It refuses to run when Perplexify is already installed on this PC, because the test uses the same product
    id and would take over (and finally remove) that installation. Use -Force only if you really want that.

.PARAMETER Installer
    The setup file to test. Default: the newest dist\Perplexify-Setup-*.exe.
.PARAMETER KeepInstalled
    Stop after the checks and leave the test installation in place (the path is printed so you can try it).
#>
[CmdletBinding()]
param(
    [string]$Installer = '',
    [switch]$KeepInstalled,
    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$Here = $PSScriptRoot
$Root = Split-Path -Parent $Here
$script:failures = 0

function Check([string]$Name, [scriptblock]$Test) {
    try {
        $result = & $Test
        if ($result -is [bool] -and -not $result) { throw 'the check returned false' }
        Write-Host ('PASS  {0}' -f $Name) -ForegroundColor Green
    } catch {
        $script:failures++
        Write-Host ('FAIL  {0}  -  {1}' -f $Name, $_.Exception.Message) -ForegroundColor Red
    }
}
function Step([string]$Text) { Write-Host ''; Write-Host "==> $Text" -ForegroundColor Cyan }

# the product id comes from the installer script, so there is one source of truth
$iss = Get-Content -LiteralPath (Join-Path $Here 'Perplexify.iss') -Raw
if ($iss -notmatch '#define AppGuid "\{\{([0-9A-Fa-f-]+)\}"') { throw 'Could not read AppGuid from Perplexify.iss' }
$Guid = '{' + $Matches[1] + '}_is1'
$UninstallKeys = @("HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$Guid",
                   "HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\$Guid",
                   "HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\$Guid")

if (-not $Installer) {
    $newest = Get-ChildItem -LiteralPath (Join-Path $Root 'dist') -Filter 'Perplexify-Setup-*.exe' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $newest) { throw 'No installer found in dist\. Run installer\build.ps1 first.' }
    $Installer = $newest.FullName
}
if (-not (Test-Path -LiteralPath $Installer)) { throw "Installer not found: $Installer" }

if (-not $Force) {
    foreach ($k in $UninstallKeys) {
        if (Test-Path -LiteralPath $k) { throw "Perplexify is already installed on this PC ($k). Uninstall it first, or pass -Force." }
    }
}

$Work = Join-Path $env:TEMP ('perplexify-install-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
$Dir  = Join-Path $Work 'Perplexify'        # a space in the path on purpose
$Data = Join-Path $Work 'data'
New-Item -ItemType Directory -Force -Path $Work, $Data | Out-Null
$Python   = Join-Path $Dir 'runtime\python.exe'
$Pythonw  = Join-Path $Dir 'runtime\pythonw.exe'
$Shortcut = Join-Path ([Environment]::GetFolderPath('Programs')) 'Perplexify.lnk'
$env:PERPLEXIFY_HOME = $Data
Write-Host "Installer : $Installer ($([math]::Round((Get-Item -LiteralPath $Installer).Length / 1MB, 1)) MB)"
Write-Host "Test dir  : $Work"

function Invoke-Setup([string]$LogName, [string]$Tasks = '') {
    $log = Join-Path $Work $LogName
    $p = Start-Process -FilePath $Installer -Wait -PassThru -ArgumentList "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR=`"$Dir`" /LOG=`"$log`" /TASKS=`"$Tasks`""
    return $p.ExitCode
}

function Get-AppProcesses {
    @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -in 'pythonw', 'python' } | Where-Object {
        try { $_.Path -and $_.Path.StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase) } catch { $false } })
}

# what the Start menu shortcut runs (read from the .lnk the installer created)
function Get-ShortcutCommand {
    $lnk = (New-Object -ComObject WScript.Shell).CreateShortcut($Shortcut)
    return @{ Target = $lnk.TargetPath; Arguments = $lnk.Arguments; WorkDir = $lnk.WorkingDirectory; Icon = $lnk.IconLocation }
}

# the Microsoft Edge WebView2 Runtime draws the app's window; without it the app opens in the browser instead
function Get-WebView2Version {
    $id = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    foreach ($k in "HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\$id", "HKLM:\SOFTWARE\Microsoft\EdgeUpdate\Clients\$id", "HKCU:\Software\Microsoft\EdgeUpdate\Clients\$id") {
        $v = Get-ItemProperty -LiteralPath $k -Name pv -ErrorAction SilentlyContinue
        if ($v -and $v.pv -and $v.pv -ne '0.0.0.0') { return $v.pv }
    }
    return $null
}
$HaveWebView2 = [bool](Get-WebView2Version)

# Without -Window the app runs the way a server does (browser mode, nothing opened), so most checks need no screen.
# With -Window it is started exactly as the Start menu shortcut starts it: in its own window.
function Start-App([switch]$Window) {
    Remove-Item -LiteralPath (Join-Path $Data 'run.json') -ErrorAction SilentlyContinue
    $cmd = Get-ShortcutCommand
    $more = if ($Window) { '' } else { ' --browser --no-browser' }
    Start-Process -FilePath $cmd.Target -ArgumentList ($cmd.Arguments + $more) -WorkingDirectory $cmd.WorkDir | Out-Null
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        $runFile = Join-Path $Data 'run.json'
        if (Test-Path -LiteralPath $runFile) {
            try {
                $info = Get-Content -LiteralPath $runFile -Raw | ConvertFrom-Json
                $h = Invoke-RestMethod -Uri "http://127.0.0.1:$($info.port)/api/health" -TimeoutSec 3
                if ($h.app -eq 'perplexify') { return [int]$info.port }
            } catch { }
        }
        Start-Sleep -Milliseconds 500
    }
    throw 'the installed app did not start within 90 seconds'
}

function Wait-AppGone([int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (@(Get-AppProcesses).Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 300
    }
    return $false
}

# the process object of the app once its window titled "Perplexify" is on screen, else $null
function Wait-AppWindow([int]$ProcessId, [int]$Seconds = 40) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $p = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if (-not $p) { return $null }
        if ($p.MainWindowHandle -ne 0 -and $p.MainWindowTitle -eq 'Perplexify') { return $p }
        Start-Sleep -Milliseconds 300
    }
    return $null
}

function Get-RunInfo { Get-Content -LiteralPath (Join-Path $Data 'run.json') -Raw | ConvertFrom-Json }

try {
    # ------------------------------------------------------------------ install
    Step 'Silent install'
    $code = Invoke-Setup 'install.log'
    Check 'installer exit code is 0' { if ($code -ne 0) { throw "exit code $code (see $Work\install.log)" } }
    foreach ($rel in 'app.ico', 'runtime\python.exe', 'runtime\pythonw.exe', 'runtime\python312._pth', 'runtime\Lib\site-packages\numpy',
                     'runtime\Lib\site-packages\onnxruntime', 'app\perplexify\__init__.py', 'app\perplexify\web\index.html', 'models', 'llama\llama-server.exe', 'llama\llama.dll', 'THIRD-PARTY-NOTICES.txt', 'unins000.exe') {
        Check "installed: $rel" { Test-Path -LiteralPath (Join-Path $Dir $rel) }
    }
    Check 'Start menu shortcut exists' { Test-Path -LiteralPath $Shortcut }
    Check 'the shortcut starts the bundled windowless Python with our icon' {
        $c = Get-ShortcutCommand
        if ($c.Target -ne $Pythonw) { throw "target is $($c.Target)" }
        if ($c.Arguments -notmatch '-m perplexify app') { throw "arguments are $($c.Arguments)" }
        if ($c.Icon -notlike '*app.ico*') { throw "icon is $($c.Icon)" }
    }
    Check 'the shortcut carries the app id that groups the taskbar button (Perplexify.Desktop)' {
        $bytes = [IO.File]::ReadAllBytes($Shortcut)       # the id sits in the .lnk's property store as UTF-16 text
        ([Text.Encoding]::Unicode.GetString($bytes) -match 'Perplexify\.Desktop') -or ([Text.Encoding]::Unicode.GetString($bytes, 1, $bytes.Length - 1) -match 'Perplexify\.Desktop')
    }
    Check 'uninstall entry is registered' { @($UninstallKeys | Where-Object { Test-Path -LiteralPath $_ }).Count -ge 1 }
    Check 'no leftover .deps-key marker' { -not (Test-Path -LiteralPath (Join-Path $Dir 'runtime\.deps-key')) }

    # ------------------------------------------------------------------ the installed program, offline
    Step 'Installed self-test (offline)'
    $env:HF_HUB_OFFLINE = '1'
    try {
        $selftest = & $Python -P -X utf8 -m perplexify selftest
        $selfOk = ($LASTEXITCODE -eq 0)
        $selftest | ForEach-Object { Write-Host "      $_" }
    } finally { Remove-Item Env:HF_HUB_OFFLINE -ErrorAction SilentlyContinue }
    Check 'selftest passes with the installed runtime' { if (-not $selfOk) { throw 'selftest failed' } }
    Check 'version command works' { (& $Python -P -m perplexify version) -match '^\d+\.\d+\.\d+$' }

    # ------------------------------------------------------------------ run the app without a window and use it
    Step 'Run the installed app (browser mode, nothing opened) and use it'
    $port = Start-App
    $base = "http://127.0.0.1:$port"
    Check 'app answers on localhost' { $port -gt 0 }
    Check 'the app runs as pythonw from the install folder' { @(Get-AppProcesses | Where-Object { $_.Name -eq 'pythonw' }).Count -ge 1 }
    Check 'the web page is served' { (Invoke-WebRequest -Uri "$base/" -UseBasicParsing).Content -match 'Perplexify' }
    Check 'bootstrap lists providers' { @((Invoke-RestMethod -Uri "$base/api/bootstrap").providers).Count -ge 3 }
    Check 'the local model engine (llama.cpp) is found in the install folder and runs' {
        $st = Invoke-RestMethod -Uri "$base/api/local/status"
        $eng = if ($st.engine) { $st.engine } else { $st }
        if (-not $eng.found) { throw 'the engine was not found' }
        if ("$($eng.source)" -ne 'installed') { throw "the engine comes from '$($eng.source)', not from the install folder" }
    }
    Check 'in browser mode the page is told so' { (Invoke-RestMethod -Uri "$base/api/bootstrap").shell -eq 'browser' }
    Check 'the installed llama-server.exe runs: --version (no model is loaded here)' {
        $exe = Join-Path $Dir 'llama\llama-server.exe'
        $so = Join-Path $Work 'llama-version.out'; $se = Join-Path $Work 'llama-version.err'
        $p = Start-Process -FilePath $exe -ArgumentList '--version' -WorkingDirectory (Split-Path -Parent $exe) -Wait -PassThru -NoNewWindow -RedirectStandardOutput $so -RedirectStandardError $se
        $said = (Get-Content -LiteralPath $so -Raw) + (Get-Content -LiteralPath $se -Raw)
        if ($p.ExitCode -ne 0) { throw "exit code $($p.ExitCode): $said" }
        if ($said -notmatch 'version:\s*\S+ \(build \d+') { throw "unexpected output: $said" }
        Write-Host "      $(($said -split "`n")[0].Trim())"
    }
    Check 'no llama-server.exe is running (the engine starts with the first question, which this test never asks)' {
        @(Get-Process -Name 'llama-server' -ErrorAction SilentlyContinue | Where-Object { try { $_.Path -and $_.Path.StartsWith($Dir, [StringComparison]::OrdinalIgnoreCase) } catch { $false } }).Count -eq 0
    }

    $sample = Join-Path $Work 'sample'
    New-Item -ItemType Directory -Force -Path $sample | Out-Null
    Set-Content -LiteralPath (Join-Path $sample 'tools.py') -Encoding UTF8 -Value @('def convert_to_wav(path, sample_rate=44100):', '    """Convert an audio file to WAV at the given sample rate."""', '    return path + ".wav"')
    Set-Content -LiteralPath (Join-Path $sample 'guide.md') -Encoding UTF8 -Value @('# Guide', '', '## Latency', 'Latency is the delay between input and output.')
    $json = @{ name = 'Install test'; embedder = @{ provider = 'fastembed'; model = 'BAAI/bge-small-en-v1.5' } } | ConvertTo-Json -Depth 5
    $lib = Invoke-RestMethod -Uri "$base/api/libraries" -Method Post -ContentType 'application/json' -Body $json
    # as a code repository: this runs the tree-sitter parser in worker processes, which must work from the windowless Python
    $job = Invoke-RestMethod -Uri "$base/api/libraries/$($lib.id)/ingest/repo" -Method Post -ContentType 'application/json' -Body (@{ path = $sample } | ConvertTo-Json)
    $final = $null
    $deadline = (Get-Date).AddSeconds(180)
    while ((Get-Date) -lt $deadline) {
        $jobs = @((Invoke-RestMethod -Uri "$base/api/jobs").jobs | Where-Object { $_.id -eq $job.id })
        if ($jobs.Count -and $jobs[0].status -notin 'queued', 'running') { $final = $jobs[0]; break }
        Start-Sleep -Seconds 1
    }
    Check 'importing a code folder (parsing in worker processes, embedding included) finishes' { if (-not $final -or $final.status -ne 'done') { throw "job ended as: $(if ($final) { $final.status + ' ' + $final.error } else { 'timeout' })" } }
    $res = Invoke-RestMethod -Uri "$base/api/search" -Method Post -ContentType 'application/json' -Body (@{ query = 'convert audio to wav'; libraries = @($lib.id); mode = 'hybrid' } | ConvertTo-Json -Depth 4)
    Check 'hybrid search finds the function' { @($res.hits | Where-Object { $_.title -like '*tools.py*' -and $_.kind -eq 'code' }).Count -ge 1 }
    Check 'search by meaning was used (embeddings exist)' { @($res.hits | Where-Object { $null -ne $_.vec_rank }).Count -ge 1 }

    # ------------------------------------------------------------------ stop
    Step 'Stop the running app'
    & $Python -P -m perplexify stop
    Check '"perplexify stop" exits 0' { $LASTEXITCODE -eq 0 }
    Check 'the app process is gone' { Wait-AppGone 20 }
    Check 'run.json was removed' { -not (Test-Path -LiteralPath (Join-Path $Data 'run.json')) }

    # ------------------------------------------------------------------ the app's own window, started exactly like the shortcut starts it
    Step "Run the installed app in its own window (WebView2 Runtime: $(if ($HaveWebView2) { Get-WebView2Version } else { 'not installed' }))"
    if (-not $HaveWebView2) {
        Write-Host 'NOTE  the Microsoft Edge WebView2 Runtime is not installed on this PC, so the window checks are skipped (the app then opens in the browser).' -ForegroundColor Yellow
    } else {
        $null = Start-App -Window
        $run = Get-RunInfo
        $base = "http://127.0.0.1:$($run.port)"
        $proc = Wait-AppWindow ([int]$run.pid)
        Check 'a window titled Perplexify opens' { if (-not $proc) { throw 'no window appeared (see the app log in the data folder)' } }
        Check 'the page is told it is in a window (not in a browser)' { (Invoke-RestMethod -Uri "$base/api/bootstrap").shell -eq 'window' }
        Check 'run.json says the app runs in a window' { $run.mode -eq 'window' }
        # a second launch (the shortcut clicked again) must bring the first window forward, not start a second copy
        $cmd = Get-ShortcutCommand
        $second = Start-Process -FilePath $cmd.Target -ArgumentList $cmd.Arguments -WorkingDirectory $cmd.WorkDir -PassThru
        Check 'a second launch hands over to the running window and exits' { if (-not $second.WaitForExit(30000)) { throw 'the second copy kept running' } }
        Check 'there is still only one copy of the app' { @(Get-AppProcesses | Where-Object { $_.Name -eq 'pythonw' }).Count -eq 1 }
        if ($proc) {
            $proc.Refresh()
            $null = $proc.CloseMainWindow()                 # the window's own close button
            Check 'closing the window quits the app' { Wait-AppGone 30 }
            Check 'run.json was removed when the window closed' { -not (Test-Path -LiteralPath (Join-Path $Data 'run.json')) }
            Check 'the window remembers its size for the next start' { Test-Path -LiteralPath (Join-Path $Data 'window.json') }
        }
    }

    # ------------------------------------------------------------------ upgrade in place while the app is running
    Step 'Upgrade in place while the app is running'
    if ($HaveWebView2) { $null = Start-App -Window } else { $null = Start-App }
    $code2 = Invoke-Setup 'upgrade.log'
    Check 'upgrade installer exit code is 0' { if ($code2 -ne 0) { throw "exit code $code2 (see $Work\upgrade.log)" } }
    Check 'the installer closed the running app first' { Wait-AppGone 10 }
    Check 'an upgrade replaced the files without leaving stale ones' { -not (Test-Path -LiteralPath (Join-Path $Dir 'runtime\.deps-key')) }
    Check 'files are intact after the upgrade' { (Test-Path -LiteralPath $Python) -and (Test-Path -LiteralPath $Pythonw) }
    Check 'the upgraded app starts again' { $p3 = Start-App; & $Python -P -m perplexify stop; $p3 -gt 0 }
    Check 'the library survived the upgrade (data is outside the install folder)' {
        $null = Start-App
        $count = @((Invoke-RestMethod -Uri "http://127.0.0.1:$((Get-Content -LiteralPath (Join-Path $Data 'run.json') -Raw | ConvertFrom-Json).port)/api/libraries").libraries).Count
        & $Python -P -m perplexify stop
        $count -ge 1
    }

    if ($KeepInstalled) {
        Write-Host ''
        Write-Host "Left installed at: $Dir" -ForegroundColor Yellow
        Write-Host "Data folder      : $Data"
        Write-Host "Remove it with   : & '$Dir\unins000.exe' /VERYSILENT"
    } else {
        # -------------------------------------------------------------- uninstall
        # The uninstaller looks for user data in %LOCALAPPDATA%\Perplexify. To prove it keeps that folder by default and
        # deletes it only with /PURGE=1, plant a canary there, but only when no such folder exists (a real one is never touched).
        $RealData = Join-Path $env:LOCALAPPDATA 'Perplexify'
        $LegacyData = Join-Path $env:LOCALAPPDATA 'ManualRAG'      # the data folder of the product's earlier name: a purge removes it too
        $script:canary = -not (Test-Path -LiteralPath $RealData)
        $script:legacyCanary = $script:canary -and -not (Test-Path -LiteralPath $LegacyData)
        if ($script:canary) {
            New-Item -ItemType Directory -Force -Path (Join-Path $RealData 'libraries') | Out-Null
            Set-Content -LiteralPath (Join-Path $RealData 'canary.txt') -Value 'delete me only when asked' -Encoding ASCII
            if ($script:legacyCanary) {
                New-Item -ItemType Directory -Force -Path $LegacyData | Out-Null
                Set-Content -LiteralPath (Join-Path $LegacyData 'canary.txt') -Value 'delete me only when asked' -Encoding ASCII
            }
        } else { Write-Host "NOTE  $RealData already exists, so the keep/purge checks are skipped to protect it." -ForegroundColor Yellow }

        Step 'Silent uninstall (keeps user data)'
        $un = Start-Process -FilePath (Join-Path $Dir 'unins000.exe') -Wait -PassThru -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART'
        Check 'uninstaller exit code is 0' { if ($un.ExitCode -ne 0) { throw "exit code $($un.ExitCode)" } }
        Start-Sleep -Seconds 2    # the uninstaller finishes by deleting itself
        Check 'program files are gone' { -not (Test-Path -LiteralPath (Join-Path $Dir 'runtime')) -and -not (Test-Path -LiteralPath (Join-Path $Dir 'app')) }
        Check 'Start menu shortcut is gone' { -not (Test-Path -LiteralPath $Shortcut) }
        Check 'uninstall entry is gone' { @($UninstallKeys | Where-Object { Test-Path -LiteralPath $_ }).Count -eq 0 }
        Check 'the app data of this test run was kept' { Test-Path -LiteralPath (Join-Path $Data 'libraries') }
        if ($script:canary) {
            Check 'a silent uninstall keeps the user data folder by default' { Test-Path -LiteralPath (Join-Path $RealData 'canary.txt') }
            if ($script:legacyCanary) { Check 'a silent uninstall keeps the data folder of the earlier name too' { Test-Path -LiteralPath (Join-Path $LegacyData 'canary.txt') } }

            Step 'Reinstall with the desktop icon, then uninstall with /PURGE=1'
            $RunKey  = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
            $Desktop = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Perplexify.lnk'
            $deskFree = -not (Test-Path -LiteralPath $Desktop)    # never touch a shortcut that was already there
            # earlier versions could start the app in the tray at sign-in; the installer must remove that entry (planted only when absent)
            $oldRunFree = $null -eq (Get-ItemProperty -Path $RunKey -Name 'Manual RAG' -ErrorAction SilentlyContinue)
            if ($oldRunFree) {
                Set-ItemProperty -Path $RunKey -Name 'Manual RAG' -Value '"C:\nowhere\pythonw.exe" -m manualrag app --no-browser'
                $script:oldRunPlanted = $true
            }
            $code3 = Invoke-Setup 'reinstall.log' 'desktopicon'
            Check 'reinstall after an uninstall works' { if ($code3 -ne 0) { throw "exit code $code3 (see $Work\reinstall.log)" } }
            if ($oldRunFree) { Check 'the installer removes the old "start when I sign in" entry' { $null -eq (Get-ItemProperty -Path $RunKey -Name 'Manual RAG' -ErrorAction SilentlyContinue) } }
            Check 'the app no longer registers itself to start at sign-in' { $null -eq (Get-ItemProperty -Path $RunKey -Name 'Perplexify' -ErrorAction SilentlyContinue) }
            if ($deskFree) { Check 'the desktop icon task creates a shortcut' { Test-Path -LiteralPath $Desktop } }
            $un2 = Start-Process -FilePath (Join-Path $Dir 'unins000.exe') -Wait -PassThru -ArgumentList '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /PURGE=1'
            Check 'uninstall with /PURGE=1 exits 0' { if ($un2.ExitCode -ne 0) { throw "exit code $($un2.ExitCode)" } }
            Start-Sleep -Seconds 2
            Check '/PURGE=1 deletes the user data folder' { -not (Test-Path -LiteralPath $RealData) }
            if ($script:legacyCanary) { Check '/PURGE=1 deletes the data folder of the earlier name too' { -not (Test-Path -LiteralPath $LegacyData) } }
            if ($deskFree) { Check 'the desktop shortcut is removed by the uninstaller' { -not (Test-Path -LiteralPath $Desktop) } }
            Check 'nothing is left registered after the second uninstall' { @($UninstallKeys | Where-Object { Test-Path -LiteralPath $_ }).Count -eq 0 }
        }
    }
}
finally {
    Remove-Item Env:PERPLEXIFY_HOME -ErrorAction SilentlyContinue
    foreach ($p in (Get-AppProcesses)) { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue }
    # remove only a canary folder this script planted
    if ((Get-Variable -Name canary -Scope Script -ErrorAction SilentlyContinue) -and $script:canary) {
        $rd = Join-Path $env:LOCALAPPDATA 'Perplexify'
        if ((Test-Path -LiteralPath (Join-Path $rd 'canary.txt')) -or ((Test-Path -LiteralPath $rd) -and -not (Get-ChildItem -LiteralPath $rd -Force | Where-Object { $_.Name -notin 'libraries', 'canary.txt' }))) {
            Remove-Item -LiteralPath $rd -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    if ((Get-Variable -Name legacyCanary -Scope Script -ErrorAction SilentlyContinue) -and $script:legacyCanary) {
        $ld = Join-Path $env:LOCALAPPDATA 'ManualRAG'
        if ((Test-Path -LiteralPath $ld) -and -not (Get-ChildItem -LiteralPath $ld -Force | Where-Object { $_.Name -ne 'canary.txt' })) {
            Remove-Item -LiteralPath $ld -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    # and the sign-in entry it planted, only while it still holds the planted value
    if ((Get-Variable -Name oldRunPlanted -Scope Script -ErrorAction SilentlyContinue) -and $script:oldRunPlanted) {
        $rv = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Manual RAG' -ErrorAction SilentlyContinue
        if ($rv -and $rv.'Manual RAG' -like '*C:\nowhere\pythonw.exe*') {
            Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'Manual RAG' -ErrorAction SilentlyContinue
        }
    }
    if (-not $KeepInstalled) { Start-Sleep -Milliseconds 500; Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue }
}

Write-Host ''
if ($script:failures -eq 0) { Write-Host 'All installer checks passed.' -ForegroundColor Green; exit 0 }
Write-Host "$($script:failures) check(s) FAILED." -ForegroundColor Red
exit 1
