#ifndef MyAppName
#define MyAppName "GasAxis Studio"
#endif
#ifndef MyAppVersion
#define MyAppVersion "0.9.0-rc5"
#endif
#ifndef MyAppPublisher
#define MyAppPublisher "GasAxis Studio"
#endif
#ifndef MyAppExeName
#define MyAppExeName "GasAxisStudio.exe"
#endif
#ifndef MyAppDir
#define MyAppDir "..\dist\GasAxisStudio"
#endif
#ifndef MyAppIcon
#define MyAppIcon "..\assets\app.ico"
#endif
#ifndef MyOutputBaseFilename
#define MyOutputBaseFilename "GasAxisStudio_Setup_x64_v" + MyAppVersion
#endif

[Setup]
AppId={{D6F34E4D-4B70-4A6C-A8D0-6F4C6A534001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\GasAxis Studio
DefaultGroupName=GasAxis Studio
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
SetupIconFile={#MyAppIcon}
OutputDir=..\dist
OutputBaseFilename={#MyOutputBaseFilename}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务"; Flags: unchecked

[Files]
Source: "{#MyAppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\GasAxis Studio"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\GasAxis Studio"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 GasAxis Studio"; Flags: nowait postinstall skipifsilent
