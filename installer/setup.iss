; Inno Setup script for Evidence Review.
; Built by build.ps1, which passes /DMyAppVersion. Requires Inno Setup 6.
;
; Per-user install by default, so no administrator prompt appears. The user may
; still choose an all-users install from the first page.

#ifndef MyAppVersion
  #define MyAppVersion "1.0.0"
#endif

#define MyAppName      "Evidence Review"
#define MyAppPublisher "Evidence Review"
#define MyAppExeName   "EvidenceReview.exe"
#define MyAppId        "{{8F3C1A42-6D19-4B57-9E2C-7A5B0D4E1C88}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription={#MyAppName} Setup

; Per-user by default; the user can elevate for an all-users install.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=no

; 64-bit only: libmpv and the PyInstaller bundle are both x64.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

OutputBaseFilename=EvidenceReviewSetup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=..\src\evidence_review\resources\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
LicenseFile=
AllowNoIcons=yes
CloseApplications=yes
RestartApplications=no

; Uncomment once a code-signing certificate is available; unsigned installers
; trigger a SmartScreen warning on first download.
; SignTool=signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "associate";   Description: "Open common video and audio files with {#MyAppName}"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
; The whole PyInstaller one-dir bundle, including _internal\mpv\mpv-2.dll.
Source: "dist\EvidenceReview\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}";          Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}";    Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; "Open with" support. HKA resolves to HKCU or HKLM to match the install scope.
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; \
    Flags: uninsdeletekey

Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".mp4"; ValueData: ""; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".mkv"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".avi"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".mov"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".wav"; ValueData: ""
Root: HKA; Subkey: "Software\Classes\Applications\{#MyAppExeName}\SupportedTypes"; \
    ValueType: string; ValueName: ".mp3"; ValueData: ""

; Add to the "Open with" list for these types when the task is selected.
Root: HKA; Subkey: "Software\Classes\.mkv\OpenWithProgids"; ValueType: string; \
    ValueName: "EvidenceReview.Media"; ValueData: ""; Tasks: associate; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\.dav\OpenWithProgids"; ValueType: string; \
    ValueName: "EvidenceReview.Media"; ValueData: ""; Tasks: associate; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\EvidenceReview.Media"; ValueType: string; \
    ValueName: ""; ValueData: "Evidence media file"; Tasks: associate; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\EvidenceReview.Media\DefaultIcon"; ValueType: string; \
    ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: associate
Root: HKA; Subkey: "Software\Classes\EvidenceReview.Media\shell\open\command"; ValueType: string; \
    ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: associate

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Start {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove only caches the app generated. The evidence database, snapshots and
; exports in %LOCALAPPDATA% are deliberately left alone.
Type: filesandordirs; Name: "{app}\_internal\__pycache__"

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    MsgBox('Evidence Review has been removed.' + #13#10#13#10 +
           'Your logged entries, captured frames and exports have been kept in:' + #13#10 +
           ExpandConstant('{localappdata}\EvidenceReview') + #13#10#13#10 +
           'Delete that folder manually if you no longer need them.',
           mbInformation, MB_OK);
  end;
end;
