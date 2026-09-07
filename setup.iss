#define MyAppName "Lap Time Sender"
#define MyAppVersion "1.4.1"
#define MyAppPublisher "Sim Coaches LLC"
#define MyAppURL "https://simcoaches.com/"
#define MyAppExeName "Lap Time Sender.exe"

[Setup]
; NOTE: The value of AppId uniquely identifies this application.
; Do not use the same AppId value in installers for other applications.
AppId={{8A4DF91C-53A7-4B71-9996-D7CE5D5BAF9F}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=installer
OutputBaseFilename=LapTimeSenderSetup
Compression=lzma
SolidCompression=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
PrivilegesRequired=lowest
WizardStyle=modern
; Signed releases: run sign_release.ps1 instead of calling ISCC directly.
; It signs the app exe first (Google Cloud KMS EV cert, same as TrackPro),
; compiles this installer around it, then signs the Setup exe.

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "acapp"; Description: "Install Assetto Corsa companion app (sector-challenge teleport + CSP OBS integration)"; GroupDescription: "Assetto Corsa integration:"
Name: "broadcast"; Description: "Set up 4th-screen broadcast camera (installs OBS Studio + CSP plugin; needs internet and one admin prompt)"; GroupDescription: "Assetto Corsa integration:"; Flags: unchecked
Name: "broadcast\autostart"; Description: "Start the broadcast screen automatically with Windows"

[Files]
; Main executable and all files in the dist folder
Source: "dist\Lap Time Sender\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; CSP companion app for Assetto Corsa (user copies it into AC's apps\lua folder)
Source: "ac_app\*"; DestDir: "{app}\ac_app"; Flags: ignoreversion recursesubdirs createallsubdirs
; Broadcast screen setup (OBS + CSP plugin, 4th-monitor exterior camera)
Source: "broadcast\*"; DestDir: "{app}\broadcast"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Rig Health Check"; Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\broadcast\check_rig.ps1"""; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
; AC companion app: copies LeaderboardSector into AC's apps\lua and enables
; CSP's OBS integration flag. Steam's folder is user-writable, so no elevation.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\ac_app\install_ac_app.ps1"" -NonInteractive"; Flags: waituntilterminated runhidden; Tasks: acapp; StatusMsg: "Installing Assetto Corsa companion app..."
; Broadcast setup runs in two stages: the machine-wide part (OBS install +
; plugin DLL into Program Files) elevated via a UAC prompt, then the per-user
; OBS scene/config as the logged-in user so it lands in their profile.
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\broadcast\setup_broadcast.ps1"" -NonInteractive -Stage plugin"; Verb: "runas"; Flags: shellexec waituntilterminated; Tasks: broadcast; StatusMsg: "Setting up broadcast screen: OBS Studio + CSP plugin..."
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\broadcast\setup_broadcast.ps1"" -NonInteractive -Stage userconfig"; Flags: waituntilterminated runhidden; Tasks: broadcast and not broadcast\autostart; StatusMsg: "Setting up broadcast screen: OBS scene..."
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\broadcast\setup_broadcast.ps1"" -NonInteractive -Stage userconfig -AutoStart"; Flags: waituntilterminated runhidden; Tasks: broadcast\autostart; StatusMsg: "Setting up broadcast screen: OBS scene + auto-start..."
Filename: "notepad.exe"; Parameters: """{app}\broadcast\README.md"""; Description: "View broadcast screen setup notes (first-use projector check)"; Flags: postinstall skipifsilent unchecked; Tasks: broadcast
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[Code]
// Optional: Add custom code for advanced installer behavior here

function GetUninstallString(): String;
var
  sUnInstPath: String;
  sUnInstallString: String;
begin
  sUnInstPath := ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{#emit SetupSetting("AppId")}_is1');
  sUnInstallString := '';
  if not RegQueryStringValue(HKLM, sUnInstPath, 'UninstallString', sUnInstallString) then
    RegQueryStringValue(HKCU, sUnInstPath, 'UninstallString', sUnInstallString);
  Result := sUnInstallString;
end;

function IsUpgrade(): Boolean;
begin
  Result := (GetUninstallString() <> '');
end;

function InitializeSetup(): Boolean;
var
  V: Integer;
  iResultCode: Integer;
  sUnInstallString: String;
begin
  Result := True;
  if IsUpgrade() then
  begin
    V := MsgBox(ExpandConstant('An older version of {#MyAppName} is already installed. Do you want to uninstall it first?'), mbInformation, MB_YESNO);
    if V = IDYES then
    begin
      sUnInstallString := GetUninstallString();
      sUnInstallString := RemoveQuotes(sUnInstallString);
      Exec(sUnInstallString, '/SILENT', '', SW_SHOW, ewWaitUntilTerminated, iResultCode);
      Result := True;
    end;
  end;
end;
