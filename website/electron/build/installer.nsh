; Kiro Crew's Windows installer remains an electron-builder assisted NSIS
; installer. This include replaces the installer's first-download pages with a
; responsive full-window branded surface while preserving the generated extraction,
; update, UAC, registry, shortcut, and uninstall machinery.

!include LogicLib.nsh
!include FileFunc.nsh
!include WinMessages.nsh
!include nsDialogs.nsh
!include x64.nsh
!include installer-messages.nsh

!define KIRO_DESIGN_WIDTH 1280
!define KIRO_DESIGN_HEIGHT 860
!define KIRO_PREF_DESKTOP "KiroInstallerDesktopShortcut"
!define KIRO_PREF_STARTUP "KiroInstallerStartWithWindows"
!define KIRO_RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define KIRO_DWMWA_USE_IMMERSIVE_DARK_MODE 20
!define KIRO_DWMWA_SYSTEMBACKDROP_TYPE 38
!define KIRO_DWMSBT_TRANSIENTWINDOW 3
!define KIRO_SPI_GETWORKAREA 0x0030
!define KIRO_GWL_STYLE -16
!define KIRO_GWL_EXSTYLE -20
!define KIRO_STYLE_MASK_NO_CHROME 0xFF3BFFFF
!define KIRO_WS_CLIPSIBLINGS 0x04000000
!define KIRO_WS_EX_TRANSPARENT 0x00000020
!define KIRO_WS_EX_LAYERED 0x00080000
!define KIRO_LWA_ALPHA 0x00000002
!define KIRO_SWP_FRAMECHANGED 0x0020
!define KIRO_SWP_NOACTIVATE 0x0010
!define KIRO_SWP_ZORDER_ONLY 0x0013
!define KIRO_HWND_BOTTOM 1
!define KIRO_HWND_TOP 0
; RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW.
!define KIRO_RDW_REFRESH 0x0181
; Add RDW_ERASE when transparent text changes so the scene repaints before the
; replacement glyphs are drawn. Without it, old and new scope notes can stack.
!define KIRO_RDW_ERASE_REFRESH 0x0185
!define KIRO_PBM_SETBARCOLOR 0x0409
!define KIRO_PBM_SETBKCOLOR 0x2001
!define KIRO_FILE_ATTRIBUTE_DIRECTORY 0x10
!define KIRO_INVALID_FILE_ATTRIBUTES -1

!ifndef BUILD_UNINSTALLER

!include StrFunc.nsh
${StrRep}

Var KiroWindowWidth
Var KiroWindowHeight
Var KiroPage
Var KiroBackground
Var KiroBackgroundHandle
Var KiroProgressPage
Var KiroProgressBackground
Var KiroProgressStatus
Var KiroProgressBar
Var KiroPrimaryFont
Var KiroTitleFont
Var KiroButtonFont
Var KiroScope
Var KiroScopeSelect
Var KiroScopeNote
Var KiroCurrentUserLabel
Var KiroAllUsersLabel
Var KiroLocationInput
Var KiroBrowseButton
Var KiroDesktopCheckbox
Var KiroStartupCheckbox
Var KiroCreateDesktopShortcut
Var KiroStartWithWindows
Var KiroInstallDir
Var KiroPerUserDefault
Var KiroPerMachineDefault
Var KiroHasPerUserInstallation
Var KiroHasPerMachineInstallation
Var KiroSkipOptions
Var KiroNativeNext
Var KiroNativeCancel
Var KiroActionButton
Var KiroExitButton
Var KiroActionLabel
Var KiroFinishLaunchCheckbox

; The design caps at the approved 1280x860 composition and shrinks to a small
; display instead of rendering off-screen. Use the desktop work area rather
; than the full monitor so a bottom or side taskbar cannot cover the installer.
; The bitmap and percentage layout scale together, including at non-100% DPI.
Function KiroConfigureWindow
  System::Alloc 16
  Pop $9
  System::Call "user32::SystemParametersInfoW(i ${KIRO_SPI_GETWORKAREA}, i 0, p r9, i 0)i.r8"
  ${If} $8 != 0
    System::Call "*$9(i.r0, i.r1, i.r2, i.r3)"
  ${Else}
    StrCpy $0 0
    StrCpy $1 0
    System::Call "user32::GetSystemMetrics(i 0)i.r2"
    System::Call "user32::GetSystemMetrics(i 1)i.r3"
  ${EndIf}
  System::Free $9

  IntOp $KiroWindowWidth $2 - $0
  IntOp $KiroWindowHeight $3 - $1
  ${If} $KiroWindowWidth > ${KIRO_DESIGN_WIDTH}
    StrCpy $KiroWindowWidth ${KIRO_DESIGN_WIDTH}
  ${EndIf}
  ${If} $KiroWindowHeight > ${KIRO_DESIGN_HEIGHT}
    StrCpy $KiroWindowHeight ${KIRO_DESIGN_HEIGHT}
  ${EndIf}

  ; Fit the approved composition inside the work area without distorting it.
  ; Independent width/height caps turn 1280x860 into 1024x720 on the hosted
  ; Windows runner, which clips the scene and separates controls from the glass.
  IntOp $8 $KiroWindowWidth * ${KIRO_DESIGN_HEIGHT}
  IntOp $9 $KiroWindowHeight * ${KIRO_DESIGN_WIDTH}
  ${If} $8 > $9
    IntOp $KiroWindowWidth $KiroWindowHeight * ${KIRO_DESIGN_WIDTH}
    IntOp $KiroWindowWidth $KiroWindowWidth / ${KIRO_DESIGN_HEIGHT}
  ${Else}
    IntOp $KiroWindowHeight $KiroWindowWidth * ${KIRO_DESIGN_HEIGHT}
    IntOp $KiroWindowHeight $KiroWindowHeight / ${KIRO_DESIGN_WIDTH}
  ${EndIf}

  IntOp $6 $2 - $0
  IntOp $6 $6 - $KiroWindowWidth
  IntOp $6 $6 / 2
  IntOp $6 $6 + $0
  IntOp $7 $3 - $1
  IntOp $7 $7 - $KiroWindowHeight
  IntOp $7 $7 / 2
  IntOp $7 $7 + $1
  ; NSIS is a 32-bit process, so use the concrete 32-bit exports rather than
  ; the pointer-sized SDK aliases, which do not have matching exports there.
  System::Call "user32::GetWindowLongW(p $HWNDPARENT, i ${KIRO_GWL_STYLE})i.r8"
  IntOp $8 $8 & ${KIRO_STYLE_MASK_NO_CHROME}
  System::Call "user32::SetWindowLongW(p $HWNDPARENT, i ${KIRO_GWL_STYLE}, i r8)i"
  System::Call "user32::SetWindowPos(p $HWNDPARENT, p 0, i r6, i r7, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_FRAMECHANGED})i"

  ; Windows 11 supplies the real system backdrop; the raster is the frosted
  ; fallback on older releases.
  StrCpy $5 ${KIRO_DWMSBT_TRANSIENTWINDOW}
  System::Call "dwmapi::DwmSetWindowAttribute(p $HWNDPARENT, i ${KIRO_DWMWA_SYSTEMBACKDROP_TYPE}, *i r5, i 4)i"
  StrCpy $5 0
  System::Call "dwmapi::DwmSetWindowAttribute(p $HWNDPARENT, i ${KIRO_DWMWA_USE_IMMERSIVE_DARK_MODE}, *i r5, i 4)i"
FunctionEnd

Function KiroHideNativeChrome
  GetDlgItem $KiroNativeNext $HWNDPARENT 1
  GetDlgItem $KiroNativeCancel $HWNDPARENT 2
  GetDlgItem $0 $HWNDPARENT 3
  ShowWindow $KiroNativeNext ${SW_HIDE}
  ShowWindow $KiroNativeCancel ${SW_HIDE}
  ShowWindow $0 ${SW_HIDE}

  ; Hide MUI header/branding siblings so they cannot overlap or retain focus.
  GetDlgItem $0 $HWNDPARENT 1028
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1034
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1035
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1036
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1037
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1038
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1039
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1045
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1046
  ShowWindow $0 ${SW_HIDE}
  GetDlgItem $0 $HWNDPARENT 1256
  ShowWindow $0 ${SW_HIDE}
FunctionEnd

Function KiroStyleControl
  Exch $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  System::Call 'uxtheme::SetWindowTheme(p r0, w "Explorer", p 0)i'
  SetCtlColors $0 0x24143C 0xF9F5FF
  Pop $0
FunctionEnd

Function KiroStyleLabel
  Exch $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $0 0x24143C transparent
  Pop $0
FunctionEnd

Function KiroColorScopeNote
  SetCtlColors $KiroScopeNote 0x5C4D6D transparent
  ShowWindow $KiroScopeNote ${SW_HIDE}
  System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ERASE_REFRESH})i"
  ShowWindow $KiroScopeNote ${SW_SHOW}
  System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ERASE_REFRESH})i"
FunctionEnd

Function KiroCreateFonts
  ; Keep the complete localized form readable when the approved composition is
  ; fitted into a small work area. Eight points is still the Windows dialog
  ; default and avoids introducing a scroll container.
  ${If} $KiroWindowWidth < 900
    CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 8 500
    CreateFont $KiroTitleFont "Segoe UI Variable Display" 10 600
    CreateFont $KiroButtonFont "Segoe UI Variable Text" 9 650
  ${Else}
    CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 10 500
    CreateFont $KiroTitleFont "Segoe UI Variable Display" 12 600
    CreateFont $KiroButtonFont "Segoe UI Variable Text" 11 650
  ${EndIf}
FunctionEnd

; Clip the scene to its native-control siblings and establish the final z-order
; only after every child exists. Otherwise the full-window bitmap can remain
; topmost and consume all input.
Function KiroEnableSiblingClipping
  Exch $0
  Push $1
  System::Call "user32::GetWindowLongW(p r0, i ${KIRO_GWL_STYLE})i.r1"
  IntOp $1 $1 | ${KIRO_WS_CLIPSIBLINGS}
  System::Call "user32::SetWindowLongW(p r0, i ${KIRO_GWL_STYLE}, i r1)i"
  Pop $1
  Pop $0
FunctionEnd

!macro KiroSinkVisual CONTROL
  ${If} ${CONTROL} != ""
  ${AndIf} ${CONTROL} != 0
    System::Call "user32::SetWindowPos(p ${CONTROL}, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  ${EndIf}
!macroend

!macro KiroCommitVisualZOrder BACKGROUND
  ; One full-window surface stays bottom-most, so independently scaled sibling
  ; sprites cannot cross native controls or introduce scaling seams.
  !insertmacro KiroSinkVisual ${BACKGROUND}
  ; The scene can paint before the transparent labels exist. Repaint every child
  ; after the final z-order is committed so those labels do not remain visually
  ; buried even though their HWNDs are now above the scene.
  System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_REFRESH})i"
!macroend

; NSD_SetStretchedImage relies on a control-relative size lookup that leaves the
; original 1280x860 bitmap clipped on a smaller MUI dialog. CopyImage receives
; the fitted pixel dimensions directly, so artwork and percentage controls keep
; the exact same coordinate system on every supported work area.
!macro KiroSetSceneImage CONTROL IMAGE HANDLE
  Push $0
  Push $1
  System::Call 'user32::LoadImageW(p 0, w "${IMAGE}", i ${IMAGE_BITMAP}, i 0, i 0, i ${LR_LOADFROMFILE})p.r0'
  System::Call 'user32::CopyImage(p r0, i ${IMAGE_BITMAP}, i $KiroWindowWidth, i $KiroWindowHeight, i ${LR_COPYDELETEORG})p.r1'
  SendMessage ${CONTROL} ${STM_SETIMAGE} ${IMAGE_BITMAP} $1
  StrCpy ${HANDLE} $1
  Pop $1
  Pop $0
!macroend

Function KiroCreateBackground
  ${NSD_CreateBitmap} 0 0 100% 100% ""
  Pop $KiroBackground
  !insertmacro KiroSetSceneImage $KiroBackground "$PLUGINSDIR\windows-installer-full-light.bmp" $KiroBackgroundHandle
  Push $KiroBackground
  Call KiroEnableSiblingClipping
FunctionEnd

Function KiroActionClicked
  Pop $0
  SendMessage $KiroNativeNext ${BM_CLICK} 0 0
FunctionEnd

Function KiroExitClicked
  Pop $0
  SendMessage $KiroNativeCancel ${BM_CLICK} 0 0
FunctionEnd

Function KiroDesktopLabelClicked
  Pop $0
  ${NSD_GetState} $KiroDesktopCheckbox $1
  ${If} $1 == ${BST_CHECKED}
    ${NSD_Uncheck} $KiroDesktopCheckbox
  ${Else}
    ${NSD_Check} $KiroDesktopCheckbox
  ${EndIf}
FunctionEnd

Function KiroStartupLabelClicked
  Pop $0
  ${NSD_GetState} $KiroStartupCheckbox $1
  ${If} $1 == ${BST_CHECKED}
    ${NSD_Uncheck} $KiroStartupCheckbox
  ${Else}
    ${NSD_Check} $KiroStartupCheckbox
  ${EndIf}
FunctionEnd

Function KiroFinishLaunchLabelClicked
  Pop $0
  ${NSD_GetState} $KiroFinishLaunchCheckbox $1
  ${If} $1 == ${BST_CHECKED}
    ${NSD_Uncheck} $KiroFinishLaunchCheckbox
  ${Else}
    ${NSD_Check} $KiroFinishLaunchCheckbox
  ${EndIf}
FunctionEnd

Function KiroCreateActionButtons
  ${NSD_CreateButton} 54.1% 92.2% 25% 5% "$KiroActionLabel"
  Pop $KiroActionButton
  SendMessage $KiroActionButton ${WM_SETFONT} $KiroButtonFont 0
  SetCtlColors $KiroActionButton 0xFFFFFF 0x6332B4
  ${NSD_OnClick} $KiroActionButton KiroActionClicked
  ${NSD_SetFocus} $KiroActionButton

  ; A regular themed Button renders as a detached gray rectangle over the
  ; artwork. This notifying label reads as part of the branded header; Escape
  ; remains the standard keyboard path through the hidden native Cancel button.
  nsDialogs::CreateControl STATIC 0x50010301 ${KIRO_WS_EX_TRANSPARENT} 83% 2.6% 15% 5% "$(kiroExitSetup)  ×"
  Pop $KiroExitButton
  SendMessage $KiroExitButton ${WM_SETFONT} $KiroPrimaryFont 0
  ${NSD_OnClick} $KiroExitButton KiroExitClicked
  SetCtlColors $KiroExitButton 0xFFFFFF transparent
FunctionEnd

Function KiroUseCurrentUser
  StrCpy $KiroScope "current"
  StrCpy $KiroInstallDir $KiroPerUserDefault
  ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${NSD_SetText} $KiroScopeNote "$(freshInstallForCurrent)"
  Call KiroColorScopeNote
  EnableWindow $KiroLocationInput 1
  EnableWindow $KiroBrowseButton 1
FunctionEnd

Function KiroUseAllUsers
  StrCpy $KiroScope "all"
  StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${NSD_SetText} $KiroScopeNote "$(freshInstallForAll)"
  Call KiroColorScopeNote
  ; A machine-wide shortcut must never point at an executable that a standard
  ; user can replace. Keep all-users installs under Program Files and make that
  ; trust boundary visible by disabling both destination controls.
  EnableWindow $KiroLocationInput 0
  EnableWindow $KiroBrowseButton 0
FunctionEnd

Function KiroScopeChanged
  Pop $0
  SendMessage $KiroScopeSelect ${CB_GETCURSEL} 0 0 $1
  ${If} $1 == 1
    Call KiroUseAllUsers
  ${Else}
    Call KiroUseCurrentUser
  ${EndIf}
FunctionEnd

Function KiroBrowseClicked
  Pop $0
  nsDialogs::SelectFolderDialog "$(^DirBrowseText)" "$KiroInstallDir"
  Pop $1
  ${If} $1 != "error"
  ${AndIf} $1 != ""
    StrCpy $KiroInstallDir $1
    ClearErrors
    Call KiroEnsureAppInstallDir
    ${If} ${Errors}
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(kiroInvalidLocation)"
      ${NSD_SetFocus} $KiroLocationInput
      Return
    ${EndIf}
    ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${EndIf}
FunctionEnd

Function KiroOptionsCreate
  ${If} $KiroSkipOptions == 1
    Abort
  ${EndIf}
  Call KiroConfigureWindow
  nsDialogs::Create 1018
  Pop $KiroPage
  ${If} $KiroPage == error
    Abort
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroPage, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  Call KiroHideNativeChrome

  Call KiroCreateFonts
  Call KiroCreateBackground

  ${NSD_CreateLabel} 19.4% 66.8% 59.7% 3.4% "$(kiroInstallOptions)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroTitleFont 0
  SetCtlColors $0 0x24143C transparent
  nsDialogs::CreateControl STATIC 0x50000300 ${KIRO_WS_EX_TRANSPARENT} 19.4% 71.3% 23.8% 3.8% "$(kiroInstallFor)"
  Pop $0
  Push $0
  Call KiroStyleLabel

  ${NSD_CreateDropList} 44.2% 70.5% 34.9% 100u ""
  Pop $KiroScopeSelect
  ReadEnvStr $0 "USERNAME"
  ${StrRep} $KiroCurrentUserLabel "$(onlyForMe)" "&" ""
  StrCpy $KiroCurrentUserLabel "$KiroCurrentUserLabel ($0)"
  ${StrRep} $KiroAllUsersLabel "$(forAll)" "&" ""
  ${NSD_CB_AddString} $KiroScopeSelect "$KiroCurrentUserLabel"
  ${NSD_CB_AddString} $KiroScopeSelect "$KiroAllUsersLabel"
  ${NSD_CB_SelectString} $KiroScopeSelect "$KiroCurrentUserLabel"
  ${NSD_OnChange} $KiroScopeSelect KiroScopeChanged
  Push $KiroScopeSelect
  Call KiroStyleControl

  ${NSD_CreateLabel} 44.2% 74.8% 34.9% 3.2% ""
  Pop $KiroScopeNote
  SendMessage $KiroScopeNote ${WM_SETFONT} $KiroPrimaryFont 0
  Call KiroColorScopeNote
  nsDialogs::CreateControl STATIC 0x50000300 ${KIRO_WS_EX_TRANSPARENT} 19.4% 78.8% 23.8% 4.2% "$(kiroInstallLocation)"
  Pop $0
  Push $0
  Call KiroStyleLabel

  ${NSD_CreateText} 44.2% 78.4% 24.1% 4.5% "$KiroInstallDir"
  Pop $KiroLocationInput
  Push $KiroLocationInput
  Call KiroStyleControl
  ${NSD_OnChange} $KiroLocationInput KiroLocationChanged
  ${NSD_CreateBrowseButton} 69.1% 78.4% 10% 4.5% "$(^BrowseBtn)"
  Pop $KiroBrowseButton
  ${NSD_OnClick} $KiroBrowseButton KiroBrowseClicked
  Push $KiroBrowseButton
  Call KiroStyleControl

  ; Create each transparent label immediately before its checkbox. The Win32
  ; accessibility proxy uses that standard sibling order as the checkbox name,
  ; while the empty glyph-only button cannot paint a duplicate text strip.
  nsDialogs::CreateControl STATIC 0x50000300 ${KIRO_WS_EX_TRANSPARENT} 21% 84% 58.1% 3.8% "$(kiroDesktopShortcut)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $0 0x24143C transparent
  ${NSD_OnClick} $0 KiroDesktopLabelClicked
  ${NSD_CreateCheckbox} 19.4% 84% 2% 3.8% ""
  Pop $KiroDesktopCheckbox
  Push $KiroDesktopCheckbox
  Call KiroStyleControl
  ${If} $KiroCreateDesktopShortcut == 1
    ${NSD_Check} $KiroDesktopCheckbox
  ${EndIf}
  nsDialogs::CreateControl STATIC 0x50000300 ${KIRO_WS_EX_TRANSPARENT} 21% 88.2% 58.1% 3.8% "$(kiroStartWithWindows)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $0 0x24143C transparent
  ${NSD_OnClick} $0 KiroStartupLabelClicked
  ${NSD_CreateCheckbox} 19.4% 88.2% 2% 3.8% ""
  Pop $KiroStartupCheckbox
  Push $KiroStartupCheckbox
  Call KiroStyleControl
  ${If} $KiroStartWithWindows == 1
    ${NSD_Check} $KiroStartupCheckbox
  ${EndIf}

  ${NSD_CreateLabel} 19.4% 93% 34% 3.5% "$(kiroReadyToInstall)"
  Pop $0
  Push $0
  Call KiroStyleLabel
  StrCpy $KiroActionLabel "$(kiroInstallAction)"
  Call KiroCreateActionButtons
  ${If} $KiroScope == "all"
    ${NSD_CB_SelectString} $KiroScopeSelect "$KiroAllUsersLabel"
    Call KiroUseAllUsers
  ${Else}
    Call KiroUseCurrentUser
  ${EndIf}
  !insertmacro KiroCommitVisualZOrder $KiroBackground
  nsDialogs::Show

  ${If} $KiroBackgroundHandle != ""
    ${NSD_FreeBitmap} $KiroBackgroundHandle
    StrCpy $KiroBackgroundHandle ""
  ${EndIf}
FunctionEnd

Function KiroLocationChanged
  Pop $0
  ${NSD_GetText} $KiroLocationInput $KiroInstallDir
FunctionEnd

; electron-builder's generated uninstaller removes $INSTDIR recursively. A
; fresh install therefore owns only a directory that did not exist before the
; install. Normalize to a product-name leaf, then keep nesting past collisions;
; checking only the leaf name would mistake an unrelated existing folder named
; Kiro Crew for an install root. Updates skip this function, so legacy custom
; paths stay in place.
Function KiroEnsureAppInstallDir
  ; A manual installer launch over an existing registration is still an
  ; update, even without electron-updater's --updated flag. Keep its registered
  ; root exactly as-is instead of treating it as a fresh directory collision.
  ${If} $KiroScope == "current"
  ${AndIf} $KiroHasPerUserInstallation == 1
    Return
  ${EndIf}
  ${If} $KiroScope == "all"
  ${AndIf} $KiroHasPerMachineInstallation == 1
    Return
  ${EndIf}

  ; GetFileName treats an existing file like a directory leaf. Walk back to
  ; the nearest existing ancestor and reject it unless it is a directory;
  ; otherwise both a direct file destination and a missing child below a file
  ; can reach electron-builder even though the target can never be created.
  StrCpy $2 $KiroInstallDir
  KiroCheckExistingInstallParent:
  System::Call 'kernel32::GetFileAttributesW(w "$2")i.r0'
  ${If} $0 != ${KIRO_INVALID_FILE_ATTRIBUTES}
    IntOp $1 $0 & ${KIRO_FILE_ATTRIBUTE_DIRECTORY}
    ${If} $1 == 0
      SetErrors
      Return
    ${EndIf}
    Goto KiroExistingInstallParentReady
  ${EndIf}
  ${GetParent} "$2" $3
  ${If} $3 == ""
  ${OrIf} $3 == $2
    Goto KiroExistingInstallParentReady
  ${EndIf}
  StrCpy $2 $3
  Goto KiroCheckExistingInstallParent

  KiroExistingInstallParentReady:

  ${GetFileName} "$KiroInstallDir" $0
  ${If} $0 != "${APP_FILENAME}"
    StrCpy $KiroInstallDir "$KiroInstallDir\${APP_FILENAME}"
  ${EndIf}

  KiroCheckFreshInstallDir:
  IfFileExists "$KiroInstallDir\*.*" KiroFreshInstallDirExists 0
  IfFileExists "$KiroInstallDir" KiroFreshInstallDirExists KiroFreshInstallDirReady

  KiroFreshInstallDirExists:
  StrCpy $KiroInstallDir "$KiroInstallDir\${APP_FILENAME}"
  Goto KiroCheckFreshInstallDir

  KiroFreshInstallDirReady:
FunctionEnd

Function KiroOptionsLeave
  ${NSD_GetText} $KiroLocationInput $KiroInstallDir
  ${If} $KiroScope == "all"
    ; Reassert the protected machine location at the elevation boundary. UI
    ; state is not a security boundary and must not be trusted here.
    StrCpy $KiroInstallDir $KiroPerMachineDefault
    ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${EndIf}
  ${If} $KiroInstallDir == ""
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(kiroInvalidLocation)"
    Abort
  ${EndIf}
  ClearErrors
  Call KiroEnsureAppInstallDir
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(kiroInvalidLocation)"
    ${NSD_SetFocus} $KiroLocationInput
    Abort
  ${EndIf}
  ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${NSD_GetState} $KiroDesktopCheckbox $KiroCreateDesktopShortcut
  ${NSD_GetState} $KiroStartupCheckbox $KiroStartWithWindows

  ${If} $KiroScope == "all"
    System::Call "shell32::IsUserAnAdmin()i.r0"
    ${If} $0 == 0
      StrCpy $0 "/allusers /kiro-options /kiro-desktop=$KiroCreateDesktopShortcut /kiro-startup=$KiroStartWithWindows /D=$KiroInstallDir"
      ClearErrors
      ExecShell "runas" "$EXEPATH" "$0"
      ${If} ${Errors}
        MessageBox MB_OK|MB_ICONSTOP "$(loginWithAdminAccount)"
        Abort
      ${EndIf}
      Quit
    ${EndIf}
  ${EndIf}
FunctionEnd

; Invisible handoff after electron-builder selects the current/all-users shell
; context. It reapplies the path chosen on the integrated page.
Function KiroApplyOptions
  ; Re-derive a machine-wide target inside the process that performs the
  ; install. Neither disabled controls nor the parent's /D argument are a trust
  ; boundary once UAC relaunches this executable.
  ${If} $KiroScope == "all"
    StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${EndIf}
  ${If} $KiroSkipOptions == 0
  ${OrIf} $KiroScope == "all"
    ClearErrors
    Call KiroEnsureAppInstallDir
    ${If} ${Errors}
      ${IfNot} ${Silent}
        MessageBox MB_OK|MB_ICONSTOP "$(kiroInvalidLocation)"
      ${EndIf}
      SetErrorLevel 2
      Quit
    ${EndIf}
  ${EndIf}
  StrCpy $INSTDIR $KiroInstallDir
  Abort
FunctionEnd

Function KiroInstallShow
  ; MUI_PAGE_FINISH normally enables this during GUI initialization. Our
  ; custom finish page replaces that macro, so restore the handoff explicitly:
  ; when InstFiles completes, NSIS advances instead of sitting at 100% forever.
  SetAutoClose true
  Call KiroConfigureWindow
  Call KiroHideNativeChrome
  FindWindow $KiroProgressPage "#32770" "" $HWNDPARENT
  ${If} $KiroProgressPage == 0
    Return
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroProgressPage, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  ; The native page and MUI header are separate children of the outer window.
  ; Keep the whole-scene bitmap at that common level so later MUI visibility
  ; changes cannot paint legacy chrome over the approved composition.
  System::Call 'user32::CreateWindowExW(i 0, w "STATIC", w "", i 0x5400000E, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, p $HWNDPARENT, p 0, p 0, p 0)p.r0'
  StrCpy $KiroProgressBackground $0
  !insertmacro KiroSetSceneImage $KiroProgressBackground "$PLUGINSDIR\windows-installer-full-light.bmp" $KiroBackgroundHandle
  Push $KiroProgressBackground
  Call KiroEnableSiblingClipping

  ; The NSIS engine obtains progress control 1004 from its original dialog and
  ; retains that HWND while extracting. Clip the dialog to that one rectangle
  ; instead of reparenting or replacing the live progress control.
  IntOp $0 $KiroWindowWidth * 1940
  IntOp $0 $0 / 10000
  IntOp $1 $KiroWindowHeight * 7440
  IntOp $1 $1 / 10000
  IntOp $2 $KiroWindowWidth * 6120
  IntOp $2 $2 / 10000
  IntOp $3 $KiroWindowHeight * 400
  IntOp $3 $3 / 10000
  System::Call 'user32::CreateWindowExW(i ${KIRO_WS_EX_TRANSPARENT}, w "STATIC", w "$(installing)", i 0x50000000, i r0, i r1, i r2, i r3, p $HWNDPARENT, p 0, p 0, p 0)p.r0'
  StrCpy $KiroProgressStatus $0
  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 11 600
  SendMessage $KiroProgressStatus ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $KiroProgressStatus 0x24143C transparent
  GetDlgItem $KiroProgressBar $KiroProgressPage 1004
  IntOp $0 $KiroWindowWidth * 1940
  IntOp $0 $0 / 10000
  IntOp $1 $KiroWindowHeight * 8260
  IntOp $1 $1 / 10000
  IntOp $2 $KiroWindowWidth * 6120
  IntOp $2 $2 / 10000
  IntOp $3 $KiroWindowHeight * 140
  IntOp $3 $3 / 10000
  IntOp $4 $0 + $2
  IntOp $5 $1 + $3
  System::Call "gdi32::CreateRectRgn(i r0, i r1, i r4, i r5)p.r6"
  System::Call "user32::SetWindowRgn(p $KiroProgressPage, p r6, i 1)i"
  System::Call "user32::SetWindowPos(p $KiroProgressBar, p ${KIRO_HWND_TOP}, i r0, i r1, i r2, i r3, i ${KIRO_SWP_NOACTIVATE})i"
  System::Call 'uxtheme::SetWindowTheme(p $KiroProgressBar, w "", w "")i'
  SendMessage $KiroProgressBar ${KIRO_PBM_SETBARCOLOR} 0 0xFF488E
  SendMessage $KiroProgressBar ${KIRO_PBM_SETBKCOLOR} 0 0xFFF5F9
  IntOp $0 $KiroWindowWidth * 8300
  IntOp $0 $0 / 10000
  IntOp $1 $KiroWindowHeight * 310
  IntOp $1 $1 / 10000
  IntOp $2 $KiroWindowWidth * 1500
  IntOp $2 $2 / 10000
  IntOp $3 $KiroWindowHeight * 420
  IntOp $3 $3 / 10000
  StrCpy $6 $0
  StrCpy $7 $1
  StrCpy $8 $2
  StrCpy $9 $3
  ; The visible caption remains a flat part of the header. A nearly transparent,
  ; layered native Cancel button occupies the same rectangle above it, so mouse,
  ; keyboard, accessibility, and the NSIS cancellation machinery stay real.
  System::Call 'user32::CreateWindowExW(i ${KIRO_WS_EX_TRANSPARENT}, w "STATIC", w "$(kiroExitSetup)  ×", i 0x50000201, i r6, i r7, i r8, i r9, p $HWNDPARENT, p 0, p 0, p 0)p.r0'
  StrCpy $KiroExitButton $0
  SendMessage $KiroExitButton ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $KiroExitButton 0xFFFFFF transparent
  SendMessage $KiroNativeCancel ${WM_SETTEXT} 0 "STR:$(kiroExitSetup)"
  System::Call "user32::GetWindowLongW(p $KiroNativeCancel, i ${KIRO_GWL_EXSTYLE})i.r4"
  IntOp $4 $4 | ${KIRO_WS_EX_LAYERED}
  System::Call "user32::SetWindowLongW(p $KiroNativeCancel, i ${KIRO_GWL_EXSTYLE}, i r4)i"
  System::Call "user32::SetLayeredWindowAttributes(p $KiroNativeCancel, i 0, i 1, i ${KIRO_LWA_ALPHA})i"
  System::Call "user32::SetWindowPos(p $KiroNativeCancel, p ${KIRO_HWND_TOP}, i r6, i r7, i r8, i r9, i ${KIRO_SWP_NOACTIVATE})i"
  ; The outer scene was created after the MUI header controls. Raise only the
  ; clipped native progress dialog and the two intentional outer controls.
  System::Call "user32::SetWindowPos(p $KiroProgressBackground, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  System::Call "user32::SetWindowPos(p $KiroProgressStatus, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  System::Call "user32::SetWindowPos(p $KiroProgressPage, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  System::Call "user32::SetWindowPos(p $KiroExitButton, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  System::Call "user32::SetWindowPos(p $KiroNativeCancel, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  ShowWindow $KiroProgressPage ${SW_SHOW}
  ShowWindow $KiroExitButton ${SW_SHOW}
  ShowWindow $KiroNativeCancel ${SW_SHOW}
  System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_REFRESH})i"
FunctionEnd

Function KiroFinishCreate
  ShowWindow $KiroExitButton ${SW_HIDE}
  System::Call "user32::DestroyWindow(p $KiroExitButton)i"
  StrCpy $KiroExitButton 0
  ShowWindow $KiroProgressPage ${SW_HIDE}
  ShowWindow $KiroProgressStatus ${SW_HIDE}
  ShowWindow $KiroProgressBackground ${SW_HIDE}
  ${If} $KiroBackgroundHandle != ""
    ${NSD_ClearBitmap} $KiroProgressBackground
    ${NSD_FreeBitmap} $KiroBackgroundHandle
    StrCpy $KiroBackgroundHandle ""
  ${EndIf}
  System::Call "user32::DestroyWindow(p $KiroProgressStatus)i"
  System::Call "user32::DestroyWindow(p $KiroProgressBackground)i"
  StrCpy $KiroProgressStatus 0
  StrCpy $KiroProgressBackground 0
  Call KiroConfigureWindow
  nsDialogs::Create 1018
  Pop $KiroPage
  ${If} $KiroPage == error
    Abort
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroPage, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  Call KiroHideNativeChrome
  Call KiroCreateFonts
  CreateFont $KiroTitleFont "Segoe UI Variable Display" 19 650
  Call KiroCreateBackground

  ${NSD_CreateLabel} 19.4% 70.5% 59.7% 6% "$(kiroInstalled)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroTitleFont 0
  SetCtlColors $0 0x24143C transparent
  nsDialogs::CreateControl STATIC 0x50000300 ${KIRO_WS_EX_TRANSPARENT} 21% 78.3% 58.1% 4.5% "$(kiroLaunchAfterFinish)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  SetCtlColors $0 0x24143C transparent
  ${NSD_OnClick} $0 KiroFinishLaunchLabelClicked
  ${NSD_CreateCheckbox} 19.4% 78.3% 2% 4.5% ""
  Pop $KiroFinishLaunchCheckbox
  Push $KiroFinishLaunchCheckbox
  Call KiroStyleControl
  ${NSD_Check} $KiroFinishLaunchCheckbox
  ; ^FinishBtn is populated by MUI_PAGE_FINISH. The custom finish page replaces
  ; that macro, so use our own complete language table instead of a blank label.
  StrCpy $KiroActionLabel "$(kiroFinishAction)"
  Call KiroCreateActionButtons
  !insertmacro KiroCommitVisualZOrder $KiroBackground
  nsDialogs::Show

  ${If} $KiroBackgroundHandle != ""
    ${NSD_FreeBitmap} $KiroBackgroundHandle
    StrCpy $KiroBackgroundHandle ""
  ${EndIf}
FunctionEnd
!endif

!macro customWelcomePage
  Page custom KiroOptionsCreate KiroOptionsLeave
!macroend

!macro customPageAfterChangeDir
  Page custom KiroApplyOptions
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroInstallShow
!macroend

!macro customFinishPage
  Function KiroFinishLeave
    ${NSD_GetState} $KiroFinishLaunchCheckbox $0
    ${If} $0 == ${BST_CHECKED}
      ${If} ${isUpdated}
        StrCpy $1 "--updated"
      ${Else}
        StrCpy $1 ""
      ${EndIf}
      ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "$1"
    ${EndIf}
  FunctionEnd
  Page custom KiroFinishCreate KiroFinishLeave
!macroend

!macro customInit
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File "${PROJECT_DIR}\..\..\packaging\installer-assets\windows-installer-full-light.bmp"

  StrCpy $KiroSkipOptions 0
  StrCpy $KiroBackgroundHandle ""
  StrCpy $KiroCreateDesktopShortcut 1
  StrCpy $KiroStartWithWindows 0
  StrCpy $KiroScope "current"
  ${If} $installMode == "all"
    StrCpy $KiroScope "all"
  ${EndIf}
  StrCpy $KiroInstallDir $INSTDIR
  StrCpy $KiroPerUserDefault "$LOCALAPPDATA\Programs\${APP_FILENAME}"
  StrCpy $KiroPerMachineDefault "$PROGRAMFILES\${APP_FILENAME}"
  StrCpy $KiroHasPerUserInstallation 0
  StrCpy $KiroHasPerMachineInstallation 0
  !ifdef APP_64
    ${If} ${RunningX64}
      StrCpy $KiroPerMachineDefault "$PROGRAMFILES64\${APP_FILENAME}"
    ${EndIf}
  !endif
  ${If} $perUserInstallationFolder != ""
    StrCpy $KiroPerUserDefault $perUserInstallationFolder
    StrCpy $KiroHasPerUserInstallation 1
  ${EndIf}
  ${If} $perMachineInstallationFolder != ""
    StrCpy $KiroPerMachineDefault $perMachineInstallationFolder
    StrCpy $KiroHasPerMachineInstallation 1
  ${EndIf}

  ; Existing installs predate startup opt-in, so a missing preference must not
  ; silently opt them in during an update.
  ${If} $KiroHasPerUserInstallation == 1
  ${OrIf} $KiroHasPerMachineInstallation == 1
    StrCpy $KiroStartWithWindows 0
  ${EndIf}
  ClearErrors
  ReadRegDWORD $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_DESKTOP}"
  ${IfNot} ${Errors}
    StrCpy $KiroCreateDesktopShortcut $0
  ${EndIf}
  ClearErrors
  ReadRegDWORD $0 SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_STARTUP}"
  ${IfNot} ${Errors}
    StrCpy $KiroStartWithWindows $0
  ${EndIf}

  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/kiro-options" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroSkipOptions 1
    StrCpy $KiroScope "all"
    ; /kiro-options is an untrusted command-line token. Ignore /D and derive
    ; the elevated destination from the protected machine root again.
    StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/kiro-desktop=" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroCreateDesktopShortcut $R1
  ${EndIf}
  ClearErrors
  ${GetOptions} $R0 "/kiro-startup=" $R1
  ${IfNot} ${Errors}
    StrCpy $KiroStartWithWindows $R1
  ${EndIf}
  ; A directly launched newer installer is also an update when electron-builder
  ; finds an existing registration. Preserve its scope, path, and preferences
  ; instead of presenting fresh-install choices that could relocate it.
  ${If} $KiroHasPerUserInstallation == 1
  ${OrIf} $KiroHasPerMachineInstallation == 1
    StrCpy $KiroSkipOptions 1
  ${EndIf}
  ${If} ${isUpdated}
    StrCpy $KiroSkipOptions 1
  ${EndIf}

  ; Custom pages do not run during a silent install, so establish the same
  ; ownership boundary here. Preserve a registered update root exactly; for a
  ; fresh per-user /D target, normalize into a previously nonexistent app leaf.
  ; Machine installs always re-derive their destination from Program Files.
  ${If} $KiroScope == "all"
    StrCpy $KiroInstallDir $KiroPerMachineDefault
  ${ElseIf} $KiroHasPerUserInstallation == 1
    StrCpy $KiroInstallDir $KiroPerUserDefault
  ${ElseIf} $KiroInstallDir == ""
    StrCpy $KiroInstallDir $KiroPerUserDefault
  ${EndIf}
  ClearErrors
  Call KiroEnsureAppInstallDir
  ${If} ${Errors}
    ${IfNot} ${Silent}
      MessageBox MB_OK|MB_ICONSTOP "$(kiroInvalidLocation)"
    ${EndIf}
    SetErrorLevel 2
    Quit
  ${EndIf}
  StrCpy $INSTDIR $KiroInstallDir
!macroend

!macro customInstallMode
  !ifndef BUILD_UNINSTALLER
    ${If} $KiroScope == "all"
      StrCpy $isForceMachineInstall 1
    ${Else}
      StrCpy $isForceCurrentInstall 1
    ${EndIf}
  !endif
!macroend

!macro customInstall
  WriteRegDWORD SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_DESKTOP}" $KiroCreateDesktopShortcut
  WriteRegDWORD SHELL_CONTEXT "${INSTALL_REGISTRY_KEY}" "${KIRO_PREF_STARTUP}" $KiroStartWithWindows
  ${If} $KiroCreateDesktopShortcut != 1
    Delete "$newDesktopLink"
  ${EndIf}
  ${If} $KiroStartWithWindows == 1
    WriteRegStr SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}" '"$appExe"'
  ${Else}
    DeleteRegValue SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}"
  ${EndIf}
!macroend

!macro customUnInstall
  DeleteRegValue SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}"
  ; The generated uninstaller clears only Roaming AppData. Remove this channel's
  ; LocalAppData updater cache on a real uninstall, never during an auto-update.
  ${ifNot} ${isUpdated}
    DetailPrint "Removing update cache: $LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
    RMDir /r "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
  ${endIf}
!macroend
