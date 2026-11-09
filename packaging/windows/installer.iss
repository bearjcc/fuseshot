; Unsigned per-user test installer for Fuseshot (Windows x64).
; Requires Inno Setup 6. Compile with build.ps1 after PyInstaller.

#define AppVersion "0.1.0-test"
#define AppName "Fuseshot"
#define Publisher "Fuseshot contributors"

[Setup]
AppId={{A6F3C2E1-9B84-4D71-9F0A-2E8C1D5B3F91}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#Publisher}
DefaultDirName={localappdata}\Programs\Fuseshot
DefaultGroupName=Fuseshot
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=Fuseshot-Setup-test
OutputDir=dist
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\Fuseshot.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "dist\Fuseshot\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "notices\*"; DestDir: "{app}\notices"; Flags: ignoreversion
Source: "..\..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\..\THIRD_PARTY.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Fuseshot"; Filename: "{app}\Fuseshot.exe"
Name: "{autodesktop}\Fuseshot"; Filename: "{app}\Fuseshot.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Fuseshot.exe"; Description: "Open Fuseshot now"; Flags: nowait postinstall skipifsilent
