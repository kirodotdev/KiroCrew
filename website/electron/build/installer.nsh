; Kiro Crew's Windows installer remains an electron-builder assisted NSIS
; installer. This include replaces the installer's first-download pages with a
; full-window, theme-aware surface while preserving the generated extraction,
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
!define KIRO_SPI_GETCLIENTAREAANIMATION 0x1042
!define KIRO_GWL_STYLE -16
!define KIRO_STYLE_MASK_NO_CHROME 0xFF3BFFFF
!define KIRO_WS_CLIPSIBLINGS 0x04000000
!define KIRO_WS_EX_TRANSPARENT 0x00000020
!define KIRO_SWP_FRAMECHANGED 0x0020
!define KIRO_SWP_NOACTIVATE 0x0010
!define KIRO_SWP_ZORDER_ONLY 0x0013
!define KIRO_HWND_BOTTOM 1
!define KIRO_HWND_TOP 0
; RDW_INVALIDATE | RDW_ALLCHILDREN | RDW_UPDATENOW. The opening animation
; changes bitmap child controls while the NSIS UI thread is intentionally
; sleeping between frames, so the children must paint before each sleep.
!define KIRO_RDW_ANIMATE 0x0181
!define KIRO_PBM_SETBARCOLOR 0x0409
!define KIRO_PBM_SETBKCOLOR 0x2001
!define KIRO_FILE_ATTRIBUTE_DIRECTORY 0x10
!define KIRO_INVALID_FILE_ATTRIBUTES -1

!ifndef BUILD_UNINSTALLER

!include StrFunc.nsh
${StrRep}

Var KiroTheme
Var KiroAnimationsEnabled
Var KiroWindowWidth
Var KiroWindowHeight
Var KiroPage
Var KiroBackground
Var KiroBackgroundHandle
Var KiroProgressPage
Var KiroProgressBackground
Var KiroProgressStatus
Var KiroProgressBar
Var KiroAnimationSurface
Var KiroProgressFrame
Var KiroOpeningSettled
Var KiroOpeningBobFrame
Var KiroTimerRunning
Var KiroPrimaryFont
Var KiroTitleFont
Var KiroButtonFont
Var KiroPrimaryColor
Var KiroMutedColor
Var KiroControlBackground
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

Function KiroDetectTheme
  StrCpy $KiroTheme "light"
  StrCpy $KiroAnimationsEnabled 1
  System::Call "user32::SystemParametersInfoW(i ${KIRO_SPI_GETCLIENTAREAANIMATION}, i 0, *i .r0, i 0)i.r1"
  ${If} $1 != 0
    StrCpy $KiroAnimationsEnabled $0
  ${EndIf}
  ClearErrors
  ReadRegDWORD $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Themes\Personalize" "AppsUseLightTheme"
  ${IfNot} ${Errors}
  ${AndIf} $0 == 0
    StrCpy $KiroTheme "dark"
  ${EndIf}

  ${If} $KiroTheme == "dark"
    StrCpy $KiroPrimaryColor 0xFFFFFF
    StrCpy $KiroMutedColor 0xE3D9F1
    StrCpy $KiroControlBackground 0x482878
  ${Else}
    StrCpy $KiroPrimaryColor 0x24143C
    StrCpy $KiroMutedColor 0x5C4D6D
    StrCpy $KiroControlBackground 0xF9F5FF
  ${EndIf}
FunctionEnd

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
  ${If} $KiroTheme == "dark"
    StrCpy $5 1
  ${EndIf}
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
  ${If} $KiroTheme == "dark"
    System::Call 'uxtheme::SetWindowTheme(p r0, w "DarkMode_Explorer", p 0)i'
    SetCtlColors $0 0xFFFFFF 0x482878
  ${Else}
    System::Call 'uxtheme::SetWindowTheme(p r0, w "Explorer", p 0)i'
    SetCtlColors $0 0x24143C 0xF9F5FF
  ${EndIf}
  Pop $0
FunctionEnd

Function KiroStyleLabel
  Exch $0
  SendMessage $0 ${WM_SETFONT} $KiroPrimaryFont 0
  ${If} $KiroTheme == "dark"
    SetCtlColors $0 0xFFFFFF transparent
  ${Else}
    SetCtlColors $0 0x24143C transparent
  ${EndIf}
  Pop $0
FunctionEnd

Function KiroColorScopeNote
  ${If} $KiroTheme == "dark"
    SetCtlColors $KiroScopeNote 0xE3D9F1 transparent
  ${Else}
    SetCtlColors $KiroScopeNote 0x5C4D6D transparent
  ${EndIf}
FunctionEnd

; Bitmap siblings repaint on every animation tick. Clip their drawing to the
; native controls and establish the final z-order only after every child exists.
; Otherwise the full-window bitmap can remain topmost and consume all input.
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
  ; Animation swaps the one full-window surface, so it can stay bottom-most
  ; without independently scaled sibling sprites crossing native controls.
  !insertmacro KiroSinkVisual ${BACKGROUND}
  ; The first animation frame can paint before the transparent labels exist.
  ; Repaint every child after the final z-order is committed so those labels do
  ; not remain visually buried even though their HWNDs are now above the scene.
  System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ANIMATE})i"
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
  ${If} $KiroTheme == "dark"
    !insertmacro KiroSetSceneImage $KiroBackground "$PLUGINSDIR\windows-installer-full-dark.bmp" $KiroBackgroundHandle
  ${Else}
    !insertmacro KiroSetSceneImage $KiroBackground "$PLUGINSDIR\windows-installer-full-light.bmp" $KiroBackgroundHandle
  ${EndIf}
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

Function KiroCreateActionButtons
  ${NSD_CreateButton} 66.6% 90.6% 12.5% 4.6% "$KiroActionLabel"
  Pop $KiroActionButton
  SendMessage $KiroActionButton ${WM_SETFONT} $KiroButtonFont 0
  ${If} $KiroTheme == "dark"
    SetCtlColors $KiroActionButton 0x2B144B 0xFFFFFF
  ${Else}
    SetCtlColors $KiroActionButton 0xFFFFFF 0x6332B4
  ${EndIf}
  ${NSD_OnClick} $KiroActionButton KiroActionClicked
  ${NSD_SetFocus} $KiroActionButton

  ${NSD_CreateButton} 89.2% 3.1% 8.3% 4.2% "$(kiroExitSetup)  ×"
  Pop $KiroExitButton
  SendMessage $KiroExitButton ${WM_SETFONT} $KiroPrimaryFont 0
  ${NSD_OnClick} $KiroExitButton KiroExitClicked
  Push $KiroExitButton
  Call KiroStyleControl
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
      MessageBox MB_OK|MB_ICONEXCLAMATION "$(^DirBrowseText)"
      ${NSD_SetFocus} $KiroLocationInput
      Return
    ${EndIf}
    ${NSD_SetText} $KiroLocationInput $KiroInstallDir
  ${EndIf}
FunctionEnd

; Reuse the opening screen's staggered character motion on every visible setup
; phase. Each frame is a complete scene so Windows performs one coherent scale
; instead of exposing seams between independently stretched bitmap crops.
Function KiroCreateOpeningAnimation
  StrCpy $KiroAnimationSurface $KiroBackground
  Call KiroStartOpeningAnimation
FunctionEnd

Function KiroPlayNativeOpeningAnimation
  StrCpy $KiroProgressFrame 0
  StrCpy $KiroOpeningSettled 0
  StrCpy $KiroOpeningBobFrame 0

  ${If} $KiroAnimationsEnabled == 0
    StrCpy $KiroOpeningSettled 1
    StrCpy $KiroProgressFrame 6
    Call KiroSetProgressFrame
    System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ANIMATE})i"
    Return
  ${EndIf}

  KiroNativeOpeningFrame:
  Call KiroSetProgressFrame
  System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ANIMATE})i"
  Sleep 120
  IntOp $KiroProgressFrame $KiroProgressFrame + 1
  ${If} $KiroProgressFrame < 5
    Goto KiroNativeOpeningFrame
  ${EndIf}
  StrCpy $KiroOpeningSettled 1
  StrCpy $KiroOpeningBobFrame 0
  StrCpy $KiroProgressFrame 6
  Call KiroSetProgressFrame
  System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ANIMATE})i"
FunctionEnd

Function KiroStartOpeningAnimation
  StrCpy $KiroProgressFrame 0
  StrCpy $KiroOpeningSettled 0
  StrCpy $KiroOpeningBobFrame 0
  ${If} $KiroAnimationsEnabled == 0
    StrCpy $KiroOpeningSettled 1
    StrCpy $KiroProgressFrame 6
    Call KiroSetProgressFrame
    Return
  ${EndIf}
  Call KiroSetProgressFrame
  ${NSD_CreateTimer} KiroAdvanceProgressFrame 150
  StrCpy $KiroTimerRunning 1
FunctionEnd

Function KiroOptionsCreate
  ${If} $KiroSkipOptions == 1
    Abort
  ${EndIf}
  Call KiroDetectTheme
  Call KiroConfigureWindow
  nsDialogs::Create 1018
  Pop $KiroPage
  ${If} $KiroPage == error
    Abort
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroPage, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  Call KiroHideNativeChrome

  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 10 500
  CreateFont $KiroTitleFont "Segoe UI Variable Display" 12 600
  CreateFont $KiroButtonFont "Segoe UI Variable Text" 11 650
  Call KiroCreateBackground
  Call KiroCreateOpeningAnimation

  ${NSD_CreateLabel} 19.4% 67.5% 24% 3.4% "$(kiroInstallOptions)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroTitleFont 0
  ${If} $KiroTheme == "dark"
    SetCtlColors $0 0xFFFFFF transparent
  ${Else}
    SetCtlColors $0 0x24143C transparent
  ${EndIf}
  ${NSD_CreateLabel} 19.4% 72.5% 15% 3.5% "$(kiroInstallFor)"
  Pop $0
  Push $0
  Call KiroStyleLabel

  ${NSD_CreateDropList} 34.7% 71.1% 44.4% 100u ""
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

  ${NSD_CreateLabel} 34.7% 76.1% 44.4% 3.2% ""
  Pop $KiroScopeNote
  SendMessage $KiroScopeNote ${WM_SETFONT} $KiroPrimaryFont 0
  Call KiroColorScopeNote
  ${NSD_CreateLabel} 19.4% 80.6% 15% 3.5% "$(kiroInstallLocation)"
  Pop $0
  Push $0
  Call KiroStyleLabel

  ${NSD_CreateText} 34.7% 79.3% 35.5% 4.5% "$KiroInstallDir"
  Pop $KiroLocationInput
  Push $KiroLocationInput
  Call KiroStyleControl
  ${NSD_OnChange} $KiroLocationInput KiroLocationChanged
  ${NSD_CreateBrowseButton} 70.8% 79.3% 8.3% 4.5% "$(^BrowseBtn)"
  Pop $KiroBrowseButton
  ${NSD_OnClick} $KiroBrowseButton KiroBrowseClicked
  Push $KiroBrowseButton
  Call KiroStyleControl

  ${NSD_CreateCheckbox} 29.7% 85.2% 22% 3.8% "$(kiroDesktopShortcut)"
  Pop $KiroDesktopCheckbox
  Push $KiroDesktopCheckbox
  Call KiroStyleLabel
  ${If} $KiroCreateDesktopShortcut == 1
    ${NSD_Check} $KiroDesktopCheckbox
  ${EndIf}
  ${NSD_CreateCheckbox} 52% 85.2% 27.1% 3.8% "$(kiroStartWithWindows)"
  Pop $KiroStartupCheckbox
  Push $KiroStartupCheckbox
  Call KiroStyleLabel
  ${If} $KiroStartWithWindows == 1
    ${NSD_Check} $KiroStartupCheckbox
  ${EndIf}

  ${NSD_CreateLabel} 19.4% 91.4% 30% 3.5% "$(kiroReadyToInstall)"
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
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(^DirBrowseText)"
    Abort
  ${EndIf}
  ClearErrors
  Call KiroEnsureAppInstallDir
  ${If} ${Errors}
    MessageBox MB_OK|MB_ICONEXCLAMATION "$(^DirBrowseText)"
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
      Call KiroStopProgressAnimation
      Quit
    ${EndIf}
  ${EndIf}
  Call KiroStopProgressAnimation
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
        MessageBox MB_OK|MB_ICONSTOP "$(^DirBrowseText)"
      ${EndIf}
      SetErrorLevel 2
      Quit
    ${EndIf}
  ${EndIf}
  StrCpy $INSTDIR $KiroInstallDir
  Abort
FunctionEnd

Function KiroSetProgressFrame
  Push $0
  ; Select the complete replacement scene before freeing the old bitmap. A
  ; clear-first swap leaves a visible gray frame that screen capture (and users
  ; on a slow machine) can observe between the two STM_SETIMAGE messages.
  StrCpy $0 $KiroBackgroundHandle
  StrCpy $KiroBackgroundHandle ""
  ${If} $KiroProgressFrame >= 6
    !insertmacro KiroSetSceneImage $KiroAnimationSurface "$PLUGINSDIR\windows-installer-full-$KiroTheme.bmp" $KiroBackgroundHandle
  ${Else}
    !insertmacro KiroSetSceneImage $KiroAnimationSurface "$PLUGINSDIR\windows-installer-progress-$KiroTheme-$KiroProgressFrame.bmp" $KiroBackgroundHandle
  ${EndIf}
  ${If} $0 != ""
    ${NSD_FreeBitmap} $0
  ${EndIf}
  Pop $0
FunctionEnd

Function KiroAdvanceProgressFrame
  ${If} $KiroOpeningSettled == 0
    IntOp $KiroProgressFrame $KiroProgressFrame + 1
    ${If} $KiroProgressFrame >= 5
      StrCpy $KiroOpeningSettled 1
      StrCpy $KiroOpeningBobFrame 0
      StrCpy $KiroProgressFrame 6
    ${EndIf}
    Call KiroSetProgressFrame
    System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ANIMATE})i"
  ${Else}
    IntOp $KiroOpeningBobFrame $KiroOpeningBobFrame + 1
    IntOp $KiroOpeningBobFrame $KiroOpeningBobFrame % 24
    ${If} $KiroOpeningBobFrame == 0
      StrCpy $KiroProgressFrame 6
      Call KiroSetProgressFrame
      System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ANIMATE})i"
    ${ElseIf} $KiroOpeningBobFrame == 12
      StrCpy $KiroProgressFrame 5
      Call KiroSetProgressFrame
      System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ANIMATE})i"
    ${EndIf}
  ${EndIf}
FunctionEnd

Function KiroStopProgressAnimation
  ${If} $KiroTimerRunning == 1
    ${NSD_KillTimer} KiroAdvanceProgressFrame
    StrCpy $KiroTimerRunning 0
  ${EndIf}
FunctionEnd

Function KiroInstallShow
  Call KiroDetectTheme
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
  ${If} $KiroTheme == "dark"
    !insertmacro KiroSetSceneImage $KiroProgressBackground "$PLUGINSDIR\windows-installer-full-dark.bmp" $KiroBackgroundHandle
  ${Else}
    !insertmacro KiroSetSceneImage $KiroProgressBackground "$PLUGINSDIR\windows-installer-full-light.bmp" $KiroBackgroundHandle
  ${EndIf}
  Push $KiroProgressBackground
  Call KiroEnableSiblingClipping
  StrCpy $KiroAnimationSurface $KiroProgressBackground

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
  ${If} $KiroTheme == "dark"
    SetCtlColors $KiroProgressStatus 0xFFFFFF transparent
  ${Else}
    SetCtlColors $KiroProgressStatus 0x24143C transparent
  ${EndIf}
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
  ${If} $KiroTheme == "dark"
    SendMessage $KiroProgressBar ${KIRO_PBM_SETBKCOLOR} 0 0x782848
  ${Else}
    SendMessage $KiroProgressBar ${KIRO_PBM_SETBKCOLOR} 0 0xFFF5F9
  ${EndIf}
  IntOp $0 $KiroWindowWidth * 8750
  IntOp $0 $0 / 10000
  IntOp $1 $KiroWindowHeight * 310
  IntOp $1 $1 / 10000
  IntOp $2 $KiroWindowWidth * 1000
  IntOp $2 $2 / 10000
  IntOp $3 $KiroWindowHeight * 420
  IntOp $3 $3 / 10000
  System::Call 'user32::SetWindowTextW(p $KiroNativeCancel, w "$(kiroExitSetup)  ×")i'
  System::Call "user32::SetWindowPos(p $KiroNativeCancel, p ${KIRO_HWND_TOP}, i r0, i r1, i r2, i r3, i ${KIRO_SWP_NOACTIVATE})i"
  SendMessage $KiroNativeCancel ${WM_SETFONT} $KiroPrimaryFont 0
  Push $KiroNativeCancel
  Call KiroStyleControl
  ; The outer scene was created after the MUI header controls. Raise only the
  ; clipped native progress dialog and the two intentional outer controls.
  System::Call "user32::SetWindowPos(p $KiroProgressBackground, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  System::Call "user32::SetWindowPos(p $KiroProgressStatus, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  System::Call "user32::SetWindowPos(p $KiroProgressPage, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  System::Call "user32::SetWindowPos(p $KiroNativeCancel, p ${KIRO_HWND_TOP}, i 0, i 0, i 0, i 0, i ${KIRO_SWP_ZORDER_ONLY})i"
  ShowWindow $KiroProgressPage ${SW_SHOW}
  ShowWindow $KiroNativeCancel ${SW_SHOW}
  System::Call "user32::RedrawWindow(p $HWNDPARENT, p 0, p 0, i ${KIRO_RDW_ANIMATE})i"
  Call KiroPlayNativeOpeningAnimation
FunctionEnd

Function KiroFinishCreate
  Call KiroStopProgressAnimation
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
  Call KiroDetectTheme
  Call KiroConfigureWindow
  nsDialogs::Create 1018
  Pop $KiroPage
  ${If} $KiroPage == error
    Abort
  ${EndIf}
  System::Call "user32::SetWindowPos(p $KiroPage, p ${KIRO_HWND_BOTTOM}, i 0, i 0, i $KiroWindowWidth, i $KiroWindowHeight, i ${KIRO_SWP_NOACTIVATE})i"
  Call KiroHideNativeChrome
  CreateFont $KiroPrimaryFont "Segoe UI Variable Text" 10 500
  CreateFont $KiroTitleFont "Segoe UI Variable Display" 19 650
  CreateFont $KiroButtonFont "Segoe UI Variable Text" 11 650
  Call KiroCreateBackground
  Call KiroCreateOpeningAnimation

  ${NSD_CreateLabel} 19.4% 70.5% 59.7% 6% "$(kiroInstalled)"
  Pop $0
  SendMessage $0 ${WM_SETFONT} $KiroTitleFont 0
  ${If} $KiroTheme == "dark"
    SetCtlColors $0 0xFFFFFF transparent
  ${Else}
    SetCtlColors $0 0x24143C transparent
  ${EndIf}
  ${NSD_CreateCheckbox} 19.4% 78.3% 59.7% 4.5% "$(kiroLaunchAfterFinish)"
  Pop $KiroFinishLaunchCheckbox
  Push $KiroFinishLaunchCheckbox
  Call KiroStyleLabel
  ${NSD_Check} $KiroFinishLaunchCheckbox
  StrCpy $KiroActionLabel "$(^FinishBtn)"
  Call KiroCreateActionButtons
  !insertmacro KiroCommitVisualZOrder $KiroBackground
  nsDialogs::Show
  Call KiroStopProgressAnimation

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
  File "${PROJECT_DIR}\..\..\packaging\installer-assets\windows-installer-full-dark.bmp"
  File "${PROJECT_DIR}\..\..\packaging\installer-assets\windows-installer-progress-*.bmp"

  StrCpy $KiroSkipOptions 0
  StrCpy $KiroTimerRunning 0
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
      MessageBox MB_OK|MB_ICONSTOP "$(^DirBrowseText)"
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
