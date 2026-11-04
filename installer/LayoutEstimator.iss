; Windows 安装包（Inno Setup 6）。在 Windows 上先用 PyInstaller 打包（desktop.spec），再：
;
;     iscc /DAppVersion=1.1.0 /DSourceDir=<repo>\dist\LayoutEstimator /O<repo>\dist installer\LayoutEstimator.iss
;
; 按当前用户安装，不需要管理员权限（公司电脑也能装）：
;   程序  %LOCALAPPDATA%\Programs\LayoutEstimator
;   数据  %LOCALAPPDATA%\LayoutEstimator（图纸库、任务；卸载时保留）
; 界面用 Edge WebView2（Windows 11 自带，Windows 10 一般已随 Edge 安装）；没有时安装结束会提示下载。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\LayoutEstimator"
#endif
#define AppName "产线投影面积估算 Layout Footprint Estimator"
#define AppExe "LayoutEstimator.exe"

[Setup]
AppId={{617DC58A-09CB-4178-9E8D-C13E0905A99B}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
DefaultDirName={autopf}\LayoutEstimator
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputBaseFilename=LayoutEstimator-{#AppVersion}-Windows-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
CloseApplications=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[InstallDelete]
; 升级时先清掉旧版本的程序文件，避免旧库与新程序混用（数据目录不在这里，不受影响）
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasVersion(Root: Integer; Key: String): Boolean;
var
  V: String;
begin
  Result := RegQueryStringValue(Root, Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0');
end;

function WebView2Installed(): Boolean;
begin
  Result := HasVersion(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key) or HasVersion(HKLM, 'SOFTWARE\' + WebView2Key)
            or HasVersion(HKCU, 'Software\' + WebView2Key);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not WebView2Installed() then
    SuppressibleMsgBox('本程序的窗口需要 Microsoft Edge WebView2 运行库，这台电脑上没有找到。' + #13#10 +
      '请从 https://go.microsoft.com/fwlink/p/?LinkId=2124703 下载安装（不需要管理员权限的版本也可以），然后再打开本程序。' + #13#10#13#10 +
      'This app needs the Microsoft Edge WebView2 Runtime, which was not found on this PC. ' +
      'Install it from https://go.microsoft.com/fwlink/p/?LinkId=2124703, then open the app.',
      mbInformation, MB_OK, IDOK);
end;
