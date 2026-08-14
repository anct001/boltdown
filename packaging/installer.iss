; Inno Setup script for Boltdown.
;
;   ISCC.exe /DMyAppVersion=0.7.0 packaging\installer.iss
;
; Input is dist\Boltdown\ as produced by packaging\boltdown.spec; output is
; dist\BoltdownSetup-<version>.exe.
;
; Two things it deliberately does *not* do:
;   * register the native messaging host - the manifest has to name the
;     extension's ID, and an unpacked extension gets a different ID on every
;     machine. The app registers it from Options -> Browser integration
;     (or `boltdown.exe --register-host <id>`), which writes to HKCU and needs
;     no elevation.
;   * install per-machine services or drivers. A download manager needs
;     neither, and staying inside HKCU keeps uninstall clean.

#define MyAppName "Boltdown"
#ifndef MyAppVersion
  #define MyAppVersion "0.7.0"
#endif
#define MyAppPublisher "Boltdown"
#define MyAppExeName "Boltdown.exe"
#define MyCliExeName "boltdown-cli.exe"
#define MyHostExeName "boltdown-host.exe"
#define SourceDir "..\dist\Boltdown"

[Setup]
AppId={{2F9C0A6E-5D3B-4C71-9E10-6A4B8D2F71C3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=..\dist
OutputBaseFilename=BoltdownSetup-{#MyAppVersion}
SetupIconFile=boltdown.ico
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
Name: "autostart"; Description: "Start Boltdown with Windows (minimised to the tray)"; GroupDescription: "Startup:"; Flags: unchecked

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
    ValueType: string; ValueName: "Boltdown"; \
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
