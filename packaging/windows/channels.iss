#ifndef StageDir
  #error StageDir is required
#endif
#ifndef OutputPath
  #define OutputPath "..\..\dist\Windows"
#endif

[Setup]
AppId={{C6C9A16B-1F7E-4A40-916D-8D9744F79E29}
AppName=视频号发布助手
AppVersion=1.12.19
AppVerName=视频号发布助手 1.12.19
AppPublisher=Social Auto Upload 本地定制版
AppPublisherURL=https://github.com/dreammis/social-auto-upload
DefaultDirName={localappdata}\Programs\SAUChannels
DefaultGroupName=视频号发布助手
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputPath}
OutputBaseFilename=视频号发布助手_安装包_v1.12.19_Win64
Compression=lzma2/normal
SolidCompression=yes
LZMANumBlockThreads=2
WizardStyle=modern
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
SetupLogging=yes
UninstallDisplayName=视频号发布助手
UninstallDisplayIcon={app}\assets\publisher-mark.ico
SetupIconFile={#StageDir}\assets\publisher-mark.ico
LicenseFile={#StageDir}\LICENSE
InfoAfterFile={#StageDir}\安装后请先阅读.txt
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "chinesesimp"; MessagesFile: "{#SourcePath}\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "快捷方式："

[Dirs]
Name: "{app}\cookies"; Flags: uninsneveruninstall
Name: "{app}\.local\gui"; Flags: uninsneveruninstall
Name: "{app}\.local\temp"

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Excludes: "logs\*,cookies\*,.local\gui\*,.local\temp\*,__pycache__\*,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\.local\gui\publish-policy.json"; DestDir: "{app}\.local\gui"; Flags: onlyifdoesntexist uninsneveruninstall

[Icons]
Name: "{group}\视频号批量发布助手"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -B ""{app}\desktop_launcher.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\publisher-mark.ico"
Name: "{group}\视频号单条发布助手"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -B ""{app}\desktop_launcher.pyw"" --single"; WorkingDir: "{app}"; IconFilename: "{app}\assets\publisher-mark.ico"
Name: "{group}\使用说明"; Filename: "{app}\安装后请先阅读.txt"
Name: "{group}\打开数据目录"; Filename: "{app}\.local\gui"
Name: "{autodesktop}\视频号批量发布助手"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -B ""{app}\desktop_launcher.pyw"""; WorkingDir: "{app}"; IconFilename: "{app}\assets\publisher-mark.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-I -B ""{app}\desktop_launcher.pyw"""; Description: "打开视频号批量发布助手"; Flags: nowait postinstall skipifsilent

; Do not add recursive UninstallDelete rules: user sessions and publishing
; receipts must survive uninstall and upgrades.
