; Perplexify - Windows installer (Inno Setup 6).
;
; Normally compiled by installer\build.ps1, which stages the app in installer\build\stage and passes the
; version and paths in as /D defines. The defaults below let you open and compile this file by hand too.
; In VS Code the "Inno Setup" extension (idleberg.innosetup) gives syntax highlighting.

#ifndef AppName
  #define AppName "Perplexify"
#endif
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef AppVersion4
  #define AppVersion4 "0.0.0.0"
#endif
#ifndef AppPublisher
  #define AppPublisher "Perplexify"
#endif
#ifndef SourceDir
  #define SourceDir "build\stage"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif
#ifndef IconFile
  #define IconFile "build\app.ico"
#endif
#ifndef VCRedist
  #define VCRedist "cache\vc_redist.x64.exe"
#endif
; the Microsoft Visual C++ runtime is installed only when the PC has an older one than this
#ifndef VCMinMajor
  #define VCMinMajor "14"
#endif
#ifndef VCMinMinor
  #define VCMinMinor "40"
#endif
; the Microsoft Edge WebView2 Runtime (Evergreen) bootstrapper, installed only when the PC has no runtime yet
#ifndef WebView2Setup
  #define WebView2Setup "cache\MicrosoftEdgeWebview2Setup.exe"
#endif

; The app is started by python.org's own windowless interpreter, so the installed files contain nothing but
; signed Python binaries, your code and the packages (no custom .exe for antivirus programs to be suspicious of).
; It opens in its own window (Microsoft Edge WebView2 draws it); closing the window quits the app.
#define AppExe "runtime\pythonw.exe"
#define AppArgs "-X utf8 -P -m perplexify app"
; Groups the taskbar button of the running window with these shortcuts. Keep it equal to APP_USER_MODEL_ID in perplexify\desktop.py.
#define AppUserModelId "Perplexify.Desktop"
; Identifies this product for upgrades and uninstalling. Never change it after you have shipped a version.
#define AppGuid "{{BDF0A5C2-09E3-4894-9B03-60C2AC375391}"

[Setup]
AppId={#AppGuid}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
VersionInfoVersion={#AppVersion4}
VersionInfoCompany={#AppPublisher}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
; Installs for the current user by default (no administrator needed); the first screen offers "all users" too.
DefaultDirName={autopf}\{#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\app.ico
SetupIconFile={#IconFile}
OutputDir={#OutputDir}
OutputBaseFilename=Perplexify-Setup-{#AppVersion}-x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
WizardStyle=modern
; A running copy is asked to quit by [Code] below; this is the fallback for anything still holding files.
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
#ifdef Fast
Compression=lzma2/fast
SolidCompression=no
#else
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
#endif
#ifdef SignIt
SignTool=signit
SignedUninstaller=yes
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: ".deps-key"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#VCRedist}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: VCRedistNeedsInstall
Source: "{#WebView2Setup}"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: WebView2NeedsInstall

[InstallDelete]
; an upgrade replaces the program folders completely, so nothing from the old version lingers
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\runtime"
; shortcuts of the product's earlier name (Manual RAG)
Type: files; Name: "{autoprograms}\Manual RAG.lnk"
Type: files; Name: "{autodesktop}\Manual RAG.lnk"

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "{#AppArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; AppUserModelID: "{#AppUserModelId}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Parameters: "{#AppArgs}"; WorkingDir: "{app}"; IconFilename: "{app}\app.ico"; AppUserModelID: "{#AppUserModelId}"; Tasks: desktopicon

[Registry]
; the app is a window now and no longer starts itself in the tray: remove the sign-in entry that earlier versions could add
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "{#AppName}"; Flags: deletevalue
Root: HKA; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "Manual RAG"; Flags: deletevalue

[Run]
Filename: "{app}\{#AppExe}"; Parameters: "{#AppArgs}"; WorkingDir: "{app}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; files the app creates inside its own folders while running (compiled Python caches)
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\models"
Type: dirifempty; Name: "{app}"

[Code]
var
  VCRuntimeFailed: Boolean;

{ ---- Microsoft Visual C++ runtime (needed by numpy, onnxruntime and friends) ---- }

function VCRedistNeedsInstall: Boolean;
var
  Installed, Major, Minor: Cardinal;
begin
  Result := True;
  if RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Installed', Installed) and (Installed = 1) and
     RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Major', Major) and
     RegQueryDWordValue(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64', 'Minor', Minor) then
    Result := (Major < {#VCMinMajor}) or ((Major = {#VCMinMajor}) and (Minor < {#VCMinMinor}));
end;

{ ---- Microsoft Edge WebView2 Runtime (draws the app's window; without it the app opens in the default browser instead) ---- }

const
  { the Evergreen WebView2 Runtime in the EdgeUpdate registry: the same place perplexify\desktop.py looks }
  WebView2ClientKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

var
  WebView2Failed: Boolean;

{ pv (REG_SZ) is the runtime version; a missing, empty or 0.0.0.0 value means "not installed" (Microsoft's rule) }
function WebView2VersionAt(const Root: Integer; const SubKey: String): String;
var
  Version: String;
begin
  Result := '';
  if RegQueryStringValue(Root, SubKey, 'pv', Version) then
  begin
    Version := Trim(Version);
    if Version <> '0.0.0.0' then
      Result := Version;
  end;
end;

function WebView2Version: String;
begin
  Result := WebView2VersionAt(HKLM32, WebView2ClientKey);       { all users: WOW6432Node on 64-bit Windows }
  if Result = '' then
    Result := WebView2VersionAt(HKLM64, WebView2ClientKey);     { not written by Microsoft's installer; the app's own check looks here too }
  if Result = '' then
    Result := WebView2VersionAt(HKCU, WebView2ClientKey);       { this user only }
end;

function WebView2NeedsInstall: Boolean;
begin
  Result := WebView2Version = '';
end;

procedure InstallWebView2;
var
  Setup: String;
  Code, Waited: Integer;
  Started: Boolean;
begin
  Setup := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
  WizardForm.StatusLabel.Caption := 'Installing the Microsoft Edge WebView2 Runtime...';
  WizardForm.ProgressGauge.Style := npbstMarquee;
  try
    { The bootstrapper's manifest says asInvoker, so only the runas verb makes Windows show its own administrator
      prompt (runtime for all users). Unlike the Visual C++ installer, an empty verb would not prompt. }
    Started := ShellExec('runas', Setup, '/silent /install', '', SW_SHOWNORMAL, ewWaitUntilTerminated, Code);
    if not Started then
    begin
      Log('WebView2: could not start the installer with administrator rights (error ' + IntToStr(Code) + ': ' +
        SysErrorMessage(Code) + '; 1223 means the prompt was declined).');
      { without administrator rights Microsoft's installer does a per-user install, which needs no prompt }
      Started := ShellExec('', Setup, '/silent /install', '', SW_SHOWNORMAL, ewWaitUntilTerminated, Code);
    end;
    if Started then
      Log('WebView2: the installer finished with exit code ' + IntToStr(Code) + '.')
    else
      Log('WebView2: could not start the installer (error ' + IntToStr(Code) + ': ' + SysErrorMessage(Code) + ').');
    { Microsoft documents no exit codes, so the registry decides; after a clean exit give it a few seconds to appear }
    Waited := 0;
    while Started and (Code = 0) and WebView2NeedsInstall and (Waited < 10000) do
    begin
      Sleep(500);
      Waited := Waited + 500;
    end;
  finally
    WizardForm.ProgressGauge.Style := npbstNormal;
  end;
  WebView2Failed := WebView2NeedsInstall;
  if WebView2Failed then
    Log('WebView2: the runtime is still missing, the app will open in the default browser.')
  else
    Log('WebView2: runtime ' + WebView2Version + ' is installed.');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    if VCRedistNeedsInstall then
    begin
      WizardForm.StatusLabel.Caption := 'Installing the Microsoft Visual C++ runtime...';
      { ShellExec lets Windows show its own administrator prompt for the runtime installer }
      if ShellExec('', ExpandConstant('{tmp}\vc_redist.x64.exe'), '/install /quiet /norestart', '', SW_SHOWNORMAL, ewWaitUntilTerminated, Code) then
        VCRuntimeFailed := not ((Code = 0) or (Code = 1638) or (Code = 3010))   { 1638: a newer one is already there; 3010: restart advised }
      else
        VCRuntimeFailed := True;
      if VCRuntimeFailed then
        Log('The Microsoft Visual C++ runtime was not installed (exit code ' + IntToStr(Code) + ').');
    end;
    if WebView2NeedsInstall then
      InstallWebView2;
  end;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID <> wpFinished then
    Exit;
  if VCRuntimeFailed then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'The Microsoft Visual C++ runtime could not be installed. If {#AppName} does not start, install it from ' +
      'https://aka.ms/vs/17/release/vc_redist.x64.exe and try again.';
  if WebView2Failed then
    WizardForm.FinishedLabel.Caption := WizardForm.FinishedLabel.Caption + #13#10#13#10 +
      'The Microsoft Edge WebView2 Runtime could not be installed, so {#AppName} will open in your web browser instead of ' +
      'its own window. For the window, install the runtime from https://developer.microsoft.com/microsoft-edge/webview2 ' +
      'and start {#AppName} again.';
end;

{ ---- a running copy must quit before its files are replaced or removed ---- }

procedure StopApp(const InstallDir: String);
var
  Python, Module: String;
  Code: Integer;
begin
  Python := AddBackslash(InstallDir) + 'runtime\python.exe';
  if not FileExists(Python) then
    Exit;
  Module := 'perplexify';
  if DirExists(AddBackslash(InstallDir) + 'app\manualrag') and not DirExists(AddBackslash(InstallDir) + 'app\perplexify') then
    Module := 'manualrag';      { a copy installed before the product was renamed }
  Exec(Python, '-P -m ' + Module + ' stop', InstallDir, SW_HIDE, ewWaitUntilTerminated, Code);
end;

function PreviousInstallDir: String;
var
  Key: String;
begin
  Result := '';
  Key := ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{#emit SetupSetting("AppId")}_is1');
  if not RegQueryStringValue(HKCU, Key, 'InstallLocation', Result) then
    if not RegQueryStringValue(HKLM, Key, 'InstallLocation', Result) then
      Result := '';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Previous: String;
begin
  Result := '';
  StopApp(WizardDirValue);
  Previous := PreviousInstallDir;
  if (Previous <> '') and (CompareText(RemoveBackslash(Previous), RemoveBackslash(WizardDirValue)) <> 0) then
    StopApp(Previous);
end;

function InitializeUninstall: Boolean;
begin
  Result := True;
  StopApp(ExpandConstant('{app}'));
end;

{ ---- the user's libraries and settings live outside the install folder and survive an uninstall unless asked otherwise ---- }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir, OldDataDir: String;
  Purge: Boolean;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  DataDir := ExpandConstant('{localappdata}\Perplexify');
  OldDataDir := ExpandConstant('{localappdata}\ManualRAG');      { the product's earlier name: the app moves it on first start }
  if not (DirExists(DataDir) or DirExists(OldDataDir)) then
    Exit;
  if UninstallSilent then
    Purge := ExpandConstant('{param:PURGE|0}') = '1'     { silent uninstall: keep the data unless /PURGE=1 is given }
  else
    Purge := MsgBox('Also delete your libraries, downloaded manuals and saved settings?' + #13#10#13#10 + DataDir + #13#10#13#10 +
      'Choose No to keep them, for example to install again later.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES;
  if Purge then
  begin
    DelTree(DataDir, True, True, True);
    DelTree(OldDataDir, True, True, True);
  end;
end;
