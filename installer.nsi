; ============================================================
; 实时语音识别系统 — NSIS 安装脚本
; 用法: makensis installer.nsi
; ============================================================

Unicode true
ManifestDPIAware true

!include "MUI2.nsh"
!include "FileFunc.nsh"

; -------------------------------------------------------------------
; 基本信息
; -------------------------------------------------------------------
Name "实时语音识别系统"
OutFile "SherpaASR-Setup.exe"
InstallDir "$PROGRAMFILES\SherpaASR"
RequestExecutionLevel admin

; -------------------------------------------------------------------
; MUI 界面
; -------------------------------------------------------------------
!define MUI_ABORTWARNING
!define MUI_ICON "${NSISDIR}\Contrib\Graphics\Icons\modern-install.ico"
!define MUI_UNICON "${NSISDIR}\Contrib\Graphics\Icons\modern-uninstall.ico"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "SimpChinese"

; -------------------------------------------------------------------
; 安装部分
; -------------------------------------------------------------------
Section "Install"
    SetOutPath "$INSTDIR"

    ; 复制所有文件（PyInstaller 输出目录）
    File /r "dist\SherpaASR\*.*"

    ; 创建 results 目录
    CreateDirectory "$INSTDIR\results"

    ; 桌面快捷方式
    CreateShortCut "$DESKTOP\语音识别系统.lnk" "$INSTDIR\sherpa-asr.exe"

    ; 开始菜单
    CreateDirectory "$SMPROGRAMS\实时语音识别系统"
    CreateShortCut "$SMPROGRAMS\实时语音识别系统\语音识别系统.lnk" "$INSTDIR\sherpa-asr.exe"
    CreateShortCut "$SMPROGRAMS\实时语音识别系统\卸载.lnk" "$INSTDIR\uninstall.exe"

    ; 写入注册表（用于添加/删除程序）
    ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
    IntFmt $0 "0x%08X" $0
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR" \
        "DisplayName" "实时语音识别系统"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR" \
        "UninstallString" '"$INSTDIR\uninstall.exe"'
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR" \
        "DisplayIcon" '"$INSTDIR\sherpa-asr.exe"'
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR" \
        "Publisher" "Sherpa ASR"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR" \
        "DisplayVersion" "1.0.0"
    WriteRegDWORD HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR" \
        "EstimatedSize" "$0"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR" \
        "NoModify" "1"
    WriteRegStr HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR" \
        "NoRepair" "1"

    WriteUninstaller "$INSTDIR\uninstall.exe"
SectionEnd

; -------------------------------------------------------------------
; 卸载部分
; -------------------------------------------------------------------
Section "Uninstall"
    ; 删除程序文件
    Delete "$INSTDIR\*.*"
    RMDir /r "$INSTDIR\_internal"
    RMDir /r "$INSTDIR\models"
    Delete "$INSTDIR\uninstall.exe"

    ; results 里是用户自己的通话录音和转写文稿，默认保留。
    ; 卸载时静默删掉是不可逆的数据销毁，所以改成显式询问、默认不删。
    IfFileExists "$INSTDIR\results\*.*" 0 no_results
        MessageBox MB_YESNO|MB_ICONEXCLAMATION \
            "是否一并删除录音与转写结果？$\r$\n$\r$\n目录：$INSTDIR\results$\r$\n$\r$\n选择“否”会保留这些文件（推荐）。" \
            /SD IDNO IDYES delete_results
        Goto no_results
    delete_results:
        RMDir /r "$INSTDIR\results"
    no_results:

    ; 只有上面没留下东西时才会真的删掉
    RMDir "$INSTDIR"

    ; 删除快捷方式
    Delete "$DESKTOP\语音识别系统.lnk"
    RMDir /r "$SMPROGRAMS\实时语音识别系统"

    ; 删除注册表
    DeleteRegKey HKLM "Software\Microsoft\Windows\CurrentVersion\Uninstall\SherpaASR"
SectionEnd
