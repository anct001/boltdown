; Inno Setup script for IDMClone.
;
;   ISCC.exe /DMyAppVersion=0.3.0 packaging\installer.iss
;
; Input is dist\IDMClone\ as produced by packaging\idmclone.spec; output is
; dist\IDMCloneSetup-<version>.exe.
;
; Two things it deliberately does *not* do:
;   * register the native messaging host - the manifest has to name the
;     extension's ID, and an unpacked extension gets a different ID on every
;     machine. The app registers it from Options -> Browser integration
;     (or `idmclone.exe --register-host <id>`), which writes to HKCU and needs
;     no elevation.
;   * install per-machine services or drivers. A download manager needs
;     neither, and staying inside HKCU keeps uninstall clean.

#define MyAppName "IDMClone"
#ifndef MyAppVersion
  #define MyAppVersion "0.3.0"
#endif
#define MyAppPublisher "IDMClone"
#define MyAppExeName "IDMClone.exe"
#define MyCliExeName "idmclone-cli.exe"
#define MyHostExeName "idmclone-host.exe"
#define SourceDir "..\dist\IDMClone"

[Setup]
AppId={{7B0F1E4C-2B0B-4E4E-9E2E-1D9A2F7C51A4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\dist
OutputBaseFilename=IDMCloneSetup-{#MyAppVersion}
SetupIconFile=idmclone.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; A 64-bit only build: the Python and Qt binaries inside are x64.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Per-user install, no elevation. Everything this app owns is per-user anyway -
; settings in %LOCALAPPDATA%, autostart and the native-messaging registration in
; HKCU - and an admin-mode install would write those under the *admin's* hive
; instead of the person who is actually going to use it.
PrivilegesRequired=lowest

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "autostart"; Description: "Start IDMClone with Windows (minimised to the tray)"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; Autostart lives in HKCU so it survives a per-user install and uninstalls cleanly.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "IDMClone"; \
    ValueData: """{app}\{#MyAppExeName}"" --tray"; \
    Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Take the native messaging registration with us; the CLI knows the keys.
Filename: "{app}\{#MyCliExeName}"; Parameters: "--unregister-host"; \
    Flags: runhidden; RunOnceId: "UnregisterHost"

[UninstallDelete]
Type: filesandordirs; Name: "{app}"
