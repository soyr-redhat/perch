[Setup]
AppName=Perch
AppVersion=0.2.0
AppId=dev.soyr.perch
DefaultDirName={localappdata}\Programs\Perch
DefaultGroupName=Perch
PrivilegesRequired=lowest
OutputBaseFilename=Perch-Setup
OutputDir=..\dist
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
[Files]
Source: "..\dist\Perch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{group}\Perch"; Filename: "{app}\Perch.exe"
Name: "{autodesktop}\Perch"; Filename: "{app}\Perch.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
[Run]
Filename: "{app}\Perch.exe"; Description: "Open Perch"; Flags: nowait postinstall skipifsilent
