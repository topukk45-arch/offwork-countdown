; Inno Setup 脚本 —— 生成带开始菜单快捷方式和卸载程序的安装包
; 用法：装 Inno Setup 6，右键本文件 Open with Inno Setup Compiler，按 F9 编译
; 前提：已经跑过 build.bat，dist\OffworkWidget\ 存在

#define MyApp      "下班倒计时"
#define MyExe      "OffworkWidget.exe"
#define MyVersion  "1.0.0"

[Setup]
AppId={{8F3C6A21-4D7E-4B90-9C15-2A6E1D0B7F44}
AppName={#MyApp}
AppVersion={#MyVersion}
DefaultDirName={autopf}\OffworkWidget
DefaultGroupName={#MyApp}
UninstallDisplayIcon={app}\{#MyExe}
OutputDir=dist
OutputBaseFilename=OffworkWidget-Setup-{#MyVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\OffworkWidget\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyApp}";              Filename: "{app}\{#MyExe}"
Name: "{group}\Uninstall {#MyApp}";    Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyApp}";        Filename: "{app}\{#MyExe}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Run]
Filename: "{app}\{#MyExe}"; Description: "Launch now"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 卸载时一并清掉配置和开机自启脚本
Type: filesandordirs; Name: "{userappdata}\OffworkWidget"
Type: files;          Name: "{userstartup}\OffworkWidget.vbs"
