; Custom NSIS include, auto-discovered by electron-builder as
; <buildResourcesDir>/installer.nsh (NsisTarget.computeCommonInstallerScriptHeader
; resolves it via packager.getResource). Its macros are inserted into the
; generated script; nothing here runs unless the macro name matches a hook the
; template inserts.
;
; Keep the assisted flow on electron-builder's native MUI pages. Page boundaries
; use one short top-level Win32 cross-fade, but extraction stays on the native
; progress page: no timer-driven bitmap swaps or Sleep calls can stall its UI
; thread. The native finish page still owns the transition away from 100%.
;
; SCOPE: exactly one directory -- the electron-updater cache under $LOCALAPPDATA,
; which the generated uninstaller cannot reach (it only ever clears $APPDATA).
;
; DELETING A DIRECTORY ANOTHER INSTALL MIGHT OWN IS THE HAZARD HERE, so the only
; path removed is one derived from THIS build's own package name. Nothing is
; removed by a hardcoded historical name: the pre-rename directories were shared
; by every channel, so an uninstall of one channel would have destroyed another
; channel's pending update download, its differential baseline, and its window
; state. Orphaned bytes are a cost; reaching into a live install is a defect.
;
; The Kiro Crew data home (~/.kiro/crew) is deliberately NOT touched: it holds
; sessions, memory and the database, is outside the install tree, and survives an
; uninstall by design (`nsis.deleteAppDataOnUninstall` stays false for the same
; reason). Neither is another product's data, e.g. %LOCALAPPDATA%\Kiro-Cli, which
; has its own installer.

!include LogicLib.nsh
!include FileFunc.nsh
!include x64.nsh

!define KIRO_RUN_KEY "Software\Microsoft\Windows\CurrentVersion\Run"
!define KIRO_FILE_ATTRIBUTE_DIRECTORY 0x10
!define KIRO_INVALID_FILE_ATTRIBUTES -1
!define KIRO_SPI_GETCLIENTAREAANIMATION 0x1042
!define KIRO_SW_HIDE 0
!define KIRO_SW_SHOW 5
!define KIRO_AW_HIDE 0x00010000
!define KIRO_AW_ACTIVATE 0x00020000
!define KIRO_AW_BLEND 0x00080000
!define KIRO_FADE_IN_MS 180
!define KIRO_FADE_OUT_MS 120

!ifndef BUILD_UNINSTALLER

Var KiroInstallDir
Var KiroPerUserDefault
Var KiroPerMachineDefault
Var KiroHasPerUserInstallation
Var KiroHasPerMachineInstallation
Var KiroScope
Var KiroAnimationsEnabled
Var KiroWindowVisible

; The fade operates on the top-level dialog, the only window type for which
; AW_BLEND is supported. It runs once per page boundary and leaves the native
; extraction page untouched while files are being installed.
Function KiroDetectAnimations
  Push $0
  Push $1
  StrCpy $KiroAnimationsEnabled 1
  System::Call "user32::SystemParametersInfoW(i ${KIRO_SPI_GETCLIENTAREAANIMATION}, i 0, *i .r0, i 0)i.r1"
  ${If} $1 != 0
    StrCpy $KiroAnimationsEnabled $0
  ${EndIf}
  Pop $1
  Pop $0
FunctionEnd

Function KiroFadeInPage
  ${If} ${Silent}
    Return
  ${EndIf}
  ${If} $KiroAnimationsEnabled == 0
    ShowWindow $HWNDPARENT ${KIRO_SW_SHOW}
    StrCpy $KiroWindowVisible 1
    Return
  ${EndIf}

  Push $0
  IntOp $0 ${KIRO_AW_ACTIVATE} | ${KIRO_AW_BLEND}
  ShowWindow $HWNDPARENT ${KIRO_SW_HIDE}
  System::Call "user32::AnimateWindow(p $HWNDPARENT, i ${KIRO_FADE_IN_MS}, i $0)i.r0"
  ${If} $0 == 0
    ShowWindow $HWNDPARENT ${KIRO_SW_SHOW}
  ${EndIf}
  StrCpy $KiroWindowVisible 1
  Pop $0
FunctionEnd

Function KiroFadeOutPage
  ${If} ${Silent}
    Return
  ${EndIf}
  ${If} $KiroAnimationsEnabled == 0
    Return
  ${EndIf}
  ${If} $KiroWindowVisible != 1
    Return
  ${EndIf}

  Push $0
  Push $1
  IntOp $0 ${KIRO_AW_HIDE} | ${KIRO_AW_BLEND}
  System::Call "user32::AnimateWindow(p $HWNDPARENT, i ${KIRO_FADE_OUT_MS}, i $0)i.r1"
  ${If} $1 == 0
    ShowWindow $HWNDPARENT ${KIRO_SW_HIDE}
  ${EndIf}
  StrCpy $KiroWindowVisible 0
  Pop $1
  Pop $0
FunctionEnd

; Cancel closes the wizard without leaving a page, so the normal page callback
; cannot observe it. The GUI lifecycle hook gives that path the same fade-out;
; the visibility guard makes it a no-op after a finish-page leave already hid it.
Function .onGUIEnd
  Call KiroFadeOutPage
FunctionEnd

; electron-builder's generated uninstaller removes $INSTDIR recursively. A
; fresh install must therefore own a directory that did not exist beforehand.
; Normalize a /D override to a product-name leaf and keep nesting past any
; collision. Registered updates retain their existing install root.
Function KiroEnsureAppInstallDir
  ${If} $KiroScope == "current"
  ${AndIf} $KiroHasPerUserInstallation == 1
    Return
  ${EndIf}
  ${If} $KiroScope == "all"
  ${AndIf} $KiroHasPerMachineInstallation == 1
    Return
  ${EndIf}

  ; Reject a direct file destination or a missing child below a file.
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

; electron-builder exposes the welcome-page macro hook directly. Reinsert the
; same native MUI page with show/leave callbacks so startup and the first Next
; transition do not blink before the rest of the fade sequence begins.
!macro customWelcomePage
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage
  !define MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroFadeOutPage
  !insertmacro MUI_PAGE_WELCOME
!macroend

!macro customPageAfterChangeDir
  ; The native install-mode page calls electron-builder's setInstallMode
  ; macros, which can replace $INSTDIR after .onInit/customInit has run.
  ; Define this callback inside the installer-only post-directory hook so its
  ; installer variables never leak into the separately compiled uninstaller.
  Function KiroValidateInstallDirAfterMode
    StrCpy $KiroScope "current"
    ${If} $installMode == "all"
      StrCpy $KiroScope "all"
    ${EndIf}
    StrCpy $KiroInstallDir $INSTDIR
    ClearErrors
    Call KiroEnsureAppInstallDir
    ${If} ${Errors}
      ${IfNot} ${Silent}
        MessageBox MB_OK|MB_ICONSTOP "$(^CantWrite)$KiroInstallDir"
      ${EndIf}
      SetErrorLevel 2
      Quit
    ${EndIf}
    StrCpy $INSTDIR $KiroInstallDir
  FunctionEnd

  Function KiroInstallModeLeave
    Call KiroValidateInstallDirAfterMode
    Call KiroFadeOutPage
  FunctionEnd

  ; These hooks apply to the immediately following native instfiles page.
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage
  !define MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroFadeOutPage
!macroend

; Attach the validator to the native install-mode page's own leave callback.
; It therefore runs after the scope macro resets $INSTDIR without adding a page
; or changing the native Install button into a misleading Next button.
!macro customInstallMode
  !ifndef BUILD_UNINSTALLER
    !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage
    !define MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroInstallModeLeave
  !endif
!macroend

; Add callbacks around the stock MUI finish page without replacing its controls,
; localization, run-after-finish behavior, or automatic progress handoff. The
; packaging contract compares this copied StartApp block with electron-builder's
; locked template so a dependency upgrade cannot silently drift from upstream.
!macro customFinishPage
  !ifndef HIDE_RUN_AFTER_FINISH
    Function StartApp
      ${if} ${isUpdated}
        StrCpy $1 "--updated"
      ${else}
        StrCpy $1 ""
      ${endif}
      ${StdUtils.ExecShellAsUser} $0 "$launchLink" "open" "$1"
    FunctionEnd

    !define MUI_FINISHPAGE_RUN
    !define MUI_FINISHPAGE_RUN_FUNCTION "StartApp"
  !endif
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage
  !define MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroFadeOutPage
  !insertmacro MUI_PAGE_FINISH
!macroend

; Preserve only the install-root ownership guard from the former custom UI.
; This runs for silent installs too and does not replace or restyle any page.
!macro customInit
  StrCpy $KiroWindowVisible 0
  Call KiroDetectAnimations
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
      MessageBox MB_OK|MB_ICONSTOP "$(^CantWrite)$KiroInstallDir"
    ${EndIf}
    SetErrorLevel 2
    Quit
  ${EndIf}
  StrCpy $INSTDIR $KiroInstallDir
!macroend

!endif

; Remove the electron-updater download cache on uninstall.
;
; WHY THE TEMPLATE CANNOT DO THIS: the generated uninstaller only ever clears
; $APPDATA (Roaming), and only when deleteAppDataOnUninstall is set. The updater
; cache lives under $LOCALAPPDATA and is named from appInfo.updaterCacheDirName,
; so no built-in path covers it.
;
; THE NAME IS DERIVED, NOT RESTATED. electron-updater resolves its cache as
; app.baseCachePath + appInfo.updaterCacheDirName, which app-builder-lib defines
; as `sanitizedName.toLowerCase() + "-updater"` over the npm package `name` --
; the same string reaching this script as ${APP_PACKAGE_NAME}. Composing it here
; is what keeps this cleanup pointed at the cache THIS build actually uses: the
; name is per-channel (build-desktop.sh overrides extraMetadata.name for
; nightly), so a derived path deletes only the uninstalling channel's cache while
; a hardcoded one would reach into whichever channel the literal happened to
; name. The lowercase step is safe to omit only because the name is already
; lowercase (npm forbids uppercase in package names), and $LOCALAPPDATA paths are
; case-insensitive regardless.
;
; THE isUpdated GUARD IS LOAD-BEARING. electron-updater runs this very
; uninstaller as part of an UPDATE (NsisUpdater.doInstall spawns the new
; installer with `--updated`, which the generated `isUpdated` flag test reads).
; The cache root holds `installer.exe`, the installer that produced the current
; install, which the NEXT update diffs against to avoid a full download
; (AppUpdater.differentialDownloadInstaller reads it as its `oldFile` baseline),
; plus a `pending/` subdirectory for an in-flight download. Deleting that on the
; update path would discard a possibly still-referenced pending download and
; force every subsequent update to transfer the whole ~200MB installer. So this
; only fires on a real user-initiated uninstall.
!macro customUnInstall
  ${ifNot} ${isUpdated}
    ; Custom installers from earlier releases could create this opt-in value.
    ; Preserve it during auto-update; only a real uninstall revokes the opt-in.
    DeleteRegValue SHELL_CONTEXT "${KIRO_RUN_KEY}" "${PRODUCT_NAME}"
    DetailPrint "Removing update cache: $LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
    RMDir /r "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
  ${endIf}
!macroend
