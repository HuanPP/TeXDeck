; Inno Setup script for the TeXDeck Windows installer. Driven by desktop/build.py:
;   ISCC /DAppVersion=x.y.z /DSourceDir=<PyInstaller TeXDeck dir> /DOutputDir=<dir> installer.iss
; Per-user install: no administrator prompt.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6C1E4E4B-6D0B-4C59-9E0B-7E5B2A9F3D11}
AppName=TeXDeck
AppVersion={#AppVersion}
AppPublisher=TeXDeck
AppPublisherURL=https://github.com/HuanPP/TeXDeck
DefaultDirName={localappdata}\Programs\TeXDeck
DefaultGroupName=TeXDeck
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir={#OutputDir}
OutputBaseFilename=TeXDeck-{#AppVersion}-windows-x64-setup
SetupIconFile={#SourcePath}\assets\icon.ico
UninstallDisplayIcon={app}\TeXDeck.exe
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\TeXDeck"; Filename: "{app}\TeXDeck.exe"
Name: "{autodesktop}\TeXDeck"; Filename: "{app}\TeXDeck.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\TeXDeck.exe"; Description: "{cm:LaunchProgram,TeXDeck}"; Flags: nowait postinstall skipifsilent
