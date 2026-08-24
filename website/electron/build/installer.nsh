; Custom NSIS include, auto-discovered by electron-builder as
; <buildResourcesDir>/installer.nsh (NsisTarget.computeCommonInstallerScriptHeader
; resolves it via packager.getResource). Its macros are inserted into the
; generated script; nothing here runs unless the macro name matches a hook the
; template inserts.
;
; Keep the assisted flow on electron-builder's native MUI pages. In particular,
; do not add custom page animations, timer-driven bitmap swaps, or Sleep calls:
; they block the NSIS UI thread and made extraction appear stalled. The native
; finish page also owns the automatic transition away from 100% completion.
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

!ifndef BUILD_UNINSTALLER

Var KiroInstallDir
Var KiroPerUserDefault
Var KiroPerMachineDefault
Var KiroHasPerUserInstallation
Var KiroHasPerMachineInstallation
Var KiroScope

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
!macroend

; Attach the validator to the native install-mode page's own leave callback.
; It therefore runs after the scope macro resets $INSTDIR without adding a page
; or changing the native Install button into a misleading Next button.
!macro customInstallMode
  !ifndef BUILD_UNINSTALLER
    !define MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroValidateInstallDirAfterMode
  !endif
!macroend

; Preserve only the install-root ownership guard from the former custom UI.
; This runs for silent installs too and does not replace or restyle any page.
!macro customInit
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
