; Custom NSIS include, auto-discovered by electron-builder as
; <buildResourcesDir>/installer.nsh (NsisTarget.computeCommonInstallerScriptHeader
; resolves it via packager.getResource). Its macros are inserted into the
; generated script; nothing here runs unless the macro name matches a hook the
; template inserts.
;
; Keep the assisted flow on electron-builder's native MUI pages. Page boundaries
; use one short top-level Win32 cross-fade, but extraction stays on the native
; progress page: no timer-driven bitmap swaps or Sleep calls can stall its UI
; thread. Fresh installs retain the native finish page. Updates skip every
; decision page, keep that progress page visible, then relaunch and close.
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
!define KIRO_WM_SETTEXT 0x000C
!define KIRO_SW_HIDE 0
!define KIRO_SW_SHOW 5
!define KIRO_AW_HIDE 0x00010000
!define KIRO_AW_ACTIVATE 0x00020000
!define KIRO_AW_BLEND 0x00080000
!define KIRO_FADE_IN_MS 180
!define KIRO_FADE_OUT_MS 120

; electron-builder ships these 26 installer languages by default. Keep the
; update-only progress contract in the same language as the native MUI chrome.
LangString KiroUpdateProgress 1033 "This can take several minutes. ${PRODUCT_NAME} will reopen automatically."
LangString KiroUpdateProgress 1031 "Dies kann mehrere Minuten dauern. ${PRODUCT_NAME} wird automatisch wieder geöffnet."
LangString KiroUpdateProgress 1036 "Cela peut prendre plusieurs minutes. ${PRODUCT_NAME} se rouvrira automatiquement."
LangString KiroUpdateProgress 3082 "Esto puede tardar varios minutos. ${PRODUCT_NAME} se volverá a abrir automáticamente."
LangString KiroUpdateProgress 2052 "这可能需要几分钟。${PRODUCT_NAME} 将自动重新打开。"
LangString KiroUpdateProgress 1028 "這可能需要幾分鐘。${PRODUCT_NAME} 將自動重新開啟。"
LangString KiroUpdateProgress 1041 "数分かかる場合があります。${PRODUCT_NAME} は自動的に再び開きます。"
LangString KiroUpdateProgress 1042 "몇 분이 걸릴 수 있습니다. ${PRODUCT_NAME}이(가) 자동으로 다시 열립니다."
LangString KiroUpdateProgress 1040 "L'operazione può richiedere alcuni minuti. ${PRODUCT_NAME} si riaprirà automaticamente."
LangString KiroUpdateProgress 1043 "Dit kan enkele minuten duren. ${PRODUCT_NAME} wordt automatisch opnieuw geopend."
LangString KiroUpdateProgress 1030 "Dette kan tage flere minutter. ${PRODUCT_NAME} åbner automatisk igen."
LangString KiroUpdateProgress 1053 "Det här kan ta flera minuter. ${PRODUCT_NAME} öppnas automatiskt igen."
LangString KiroUpdateProgress 1044 "Dette kan ta flere minutter. ${PRODUCT_NAME} åpnes automatisk igjen."
LangString KiroUpdateProgress 1035 "Tämä voi kestää useita minuutteja. ${PRODUCT_NAME} avautuu automaattisesti uudelleen."
LangString KiroUpdateProgress 1049 "Это может занять несколько минут. ${PRODUCT_NAME} откроется снова автоматически."
LangString KiroUpdateProgress 2070 "Isto pode demorar vários minutos. ${PRODUCT_NAME} será reaberto automaticamente."
LangString KiroUpdateProgress 1046 "Isto pode demorar vários minutos. ${PRODUCT_NAME} será reaberto automaticamente."
LangString KiroUpdateProgress 1045 "Może to potrwać kilka minut. ${PRODUCT_NAME} otworzy się ponownie automatycznie."
LangString KiroUpdateProgress 1058 "Це може тривати кілька хвилин. ${PRODUCT_NAME} автоматично відкриється знову."
LangString KiroUpdateProgress 1029 "Může to trvat několik minut. ${PRODUCT_NAME} se automaticky znovu otevře."
LangString KiroUpdateProgress 1051 "Môže to trvať niekoľko minút. ${PRODUCT_NAME} sa automaticky znova otvorí."
LangString KiroUpdateProgress 1038 "Ez több percet is igénybe vehet. A(z) ${PRODUCT_NAME} automatikusan újra megnyílik."
LangString KiroUpdateProgress 1025 "قد يستغرق هذا عدة دقائق. سيُعاد فتح ${PRODUCT_NAME} تلقائيًا."
LangString KiroUpdateProgress 1055 "Bu işlem birkaç dakika sürebilir. ${PRODUCT_NAME} otomatik olarak yeniden açılacaktır."
LangString KiroUpdateProgress 1054 "อาจใช้เวลาหลายนาที ${PRODUCT_NAME} จะเปิดขึ้นอีกครั้งโดยอัตโนมัติ"
LangString KiroUpdateProgress 1066 "Quá trình này có thể mất vài phút. ${PRODUCT_NAME} sẽ tự động mở lại."

!ifndef BUILD_UNINSTALLER

Var KiroInstallDir
Var KiroPerUserDefault
Var KiroPerMachineDefault
Var KiroHasPerUserInstallation
Var KiroHasPerMachineInstallation
Var KiroScope
Var KiroAnimationsEnabled
Var KiroWindowVisible
Var KiroVisibleUpdate
; The app executable's filename, carried as a VARIABLE rather than referenced as
; ${APP_EXECUTABLE_FILENAME} where it is used. That define comes from the
; template's common.nsh, which is included AFTER this file, so a Function body --
; compiled at !include time -- cannot see it: NSIS emits `warning 6000: unknown
; variable/constant "{APP_EXECUTABLE_FILENAME}"` and silently drops it, which
; would reduce an ownership test to a bare directory path. A !macro body is
; expanded at its !insertmacro site and CAN see it, so customInit assigns this
; variable and the Function reads it. (${APP_FILENAME} is fine at include time --
; it comes from the generated header, not common.nsh.)
Var KiroAppExeName
; Bifurcation evidence for the shortcut heal in customInstall: whether a
; sibling install root (the shape the former collision-nesting produced)
; physically exists beside the root this update writes.
Var KiroStaleSibling
Var KiroStaleSiblingProbe

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
;
; AN UPDATE NEVER RELOCATES THE INSTALL, and that is checked FIRST -- before the
; registry-derived guards below, not through them. electron-updater re-runs this
; installer with `--updated` against an install that by definition already
; exists, so the collision-nesting further down would resolve $INSTDIR to a
; FRESH subdirectory and install the new version beside the running one instead
; of over it. The result is two parallel installs: the update lands in one, the
; shortcuts and the post-install relaunch keep pointing at the other, and the
; stale copy re-discovers the very same update on its next feed check -- an
; update loop the user cannot escape except by launching the new path by hand.
;
; The two guards below cannot carry this, because both are downstream of ONE
; registry value: upstream's initMultiUser reads `InstallLocation` from
; ${INSTALL_REGISTRY_KEY} into $perUserInstallationFolder /
; $perMachineInstallationFolder, and customInit turns those into
; $KiroHasPerUserInstallation / $KiroHasPerMachineInstallation. When that value
; is missing both flags read 0, the guards fall through, and the update nests.
; That is not hypothetical: on a machine that hit this loop the uninstall key
; carried DisplayVersion, UninstallString and DisplayIcon all pointing at the
; real install root while `InstallLocation` itself was absent. Why it was absent
; is not established -- registryAddInstallInfo writes it unconditionally -- which
; is exactly why the update path must not depend on it being there.
;
; The guard tests $KiroVisibleUpdate rather than ${isUpdated}, and that is NOT
; interchangeable here. ${isUpdated} expands to a StdUtils::TestParameter plugin
; call, and this is a Function, so its body is compiled when this file is
; !included -- before the generated script runs !addplugindir. Writing
; ${isUpdated} here fails the build at compile time with "Plugin not found,
; cannot call StdUtils::TestParameter". customInit gets away with it because a
; !macro body is only expanded at its !insertmacro site, which is late enough.
; customInit sets $KiroVisibleUpdate from ${isUpdated} before it calls this
; function, and the install-mode page's leave callback runs later still, so the
; variable is populated on both call paths.
;
; The update bypass is NOT unconditional, and must not be made so. `--updated` is
; a command-line flag on a user-runnable installer, so it can arrive alongside
; `/D=<any existing directory>`; an unconditional bypass would then adopt a
; directory this installer never created, record it as the install root, and the
; generated uninstaller -- which removes $INSTDIR recursively -- would delete the
; user's pre-existing contents there on uninstall. So the bypass requires proof
; that the target IS one of our install roots, and the proof is the presence of
; this app's own executable in it. Path equality against the canonical default is
; deliberately not used as the test: a path can match while the directory belongs
; to something else, whereas our executable being there cannot. A registered root
; is still short-circuited by the $KiroHasPer*Installation guards below.
;
; $KiroAppExeName, not ${APP_EXECUTABLE_FILENAME}: that define is unavailable at
; !include time, and NSIS drops it with `warning 6000` instead of failing loudly,
; which would silently reduce this test to a bare directory path and let the
; bypass fire for a directory we do not own. See the Var declaration.
;
; What remains covered: the loop this fixes needs an update whose install root is
; already a Kiro Crew install, which is exactly the case the executable check
; admits. An update that cannot prove ownership falls through to the ordinary
; fresh-install ownership path instead of adopting the directory.
;
; $KiroAppExeName is also tested for non-emptiness, BEFORE it is appended.
; FileExists matches a directory as readily as a file, so an empty name collapses
; the ownership proof into `FileExists "$KiroInstallDir\"` -- a bare directory
; test, which is the same "reduce this test to a bare directory path and let the
; bypass fire for a directory we do not own" outcome the Var declaration warns
; about, reached by a different route. That warning covers the
; ${APP_EXECUTABLE_FILENAME} include-time hazard; this covers the variable simply
; never having been assigned, since only customInit assigns it. Without the test,
; every safety property here rests on customInit having run first -- true on both
; call paths today, and not something a page callback inserted ahead of it should
; be able to quietly invalidate. The cost of being wrong is not a failed update:
; the generated uninstaller removes $INSTDIR recursively, so adopting a directory
; we did not create deletes whatever the user already had there.
Function KiroEnsureAppInstallDir
  ${If} $KiroVisibleUpdate == 1
  ${AndIf} $KiroAppExeName != ""
  ${AndIf} ${FileExists} "$KiroInstallDir\$KiroAppExeName"
    Return
  ${EndIf}
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

; Updates need progress, not another decision: skip the welcome page while
; preserving the native assisted flow for a first install.
Function KiroWelcomePre
  ${If} $KiroVisibleUpdate == 1
    Abort
  ${EndIf}
FunctionEnd

; A visible update must stay non-interactive while files are being replaced.
; Hide the native Cancel button and reject window-close/Alt+F4 aborts; cancelling
; midway can leave the installed app only partially replaced.
Function KiroInstFilesShow
  Call KiroFadeInPage
  ${If} $KiroVisibleUpdate == 1
    ; The native progress page is the only surface that is guaranteed to remain
    ; visible throughout the handoff. Put the timing/relaunch contract on that
    ; page itself instead of relying on a toast that Focus Assist may suppress.
    GetDlgItem $0 $HWNDPARENT 1038
    SendMessage $0 ${KIRO_WM_SETTEXT} 0 "STR:$(KiroUpdateProgress)"
    GetDlgItem $0 $HWNDPARENT 2
    EnableWindow $0 0
    ShowWindow $0 ${KIRO_SW_HIDE}
  ${EndIf}
FunctionEnd

; MUI owns .onUserAbort, so use its pre-warning hook instead of replacing the
; generated callback. Returning preserves the normal confirmation on a fresh
; install; Abort suppresses both the warning and the close during an update.
Function KiroAbortPre
  ${If} $KiroVisibleUpdate == 1
    Abort
  ${EndIf}
FunctionEnd
!define MUI_PAGE_FUNCTION_ABORTWARNING KiroAbortPre

; electron-builder exposes the welcome-page macro hook directly. Reinsert the
; same native MUI page with show/leave callbacks so startup and the first Next
; transition do not blink before the rest of the fade sequence begins.
!macro customWelcomePage
  !define MUI_PAGE_CUSTOMFUNCTION_PRE KiroWelcomePre
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
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroInstFilesShow
  !define MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroFadeOutPage
!macroend

; Attach the validator to the native install-mode page's own leave callback.
; It therefore runs after the scope macro resets $INSTDIR without adding a page
; or changing the native Install button into a misleading Next button.
!macro customInstallMode
  !ifndef BUILD_UNINSTALLER
    ; Preserve the registered install scope without asking the user to choose it
    ; again. multiUserUi consumes these flags in its page-pre callback, performs
    ; elevation when needed, and Abort-skips the page.
    ${If} $KiroVisibleUpdate == 1
      ${If} $installMode == "all"
        StrCpy $isForceMachineInstall 1
      ${Else}
        StrCpy $isForceCurrentInstall 1
      ${EndIf}
    ${EndIf}
    !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage
    !define MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroInstallModeLeave
  !endif
!macroend

; Add callbacks around the stock MUI finish page without replacing its controls,
; localization, run-after-finish behavior, or automatic progress handoff.
;
; StartApp DELIBERATELY DIVERGES from electron-builder's template in exactly one
; expression, and the packaging contract asserts that divergence instead of
; equality so a dependency upgrade still cannot change it silently. Upstream
; launches "$launchLink", which installSection.nsh resolves to $newStartMenuLink
; whenever that shortcut exists and only falls back to the installed executable
; when it does not. A shortcut is a POINTER, and on an update it is not
; necessarily repointed: registryAddInstallInfo records KeepShortcuts="true", so
; addStartMenuLink keeps whatever the previous install left behind. When that
; shortcut names a different install root than the one this run just wrote --
; exactly what the nesting bug guarded against above produces -- the relaunch
; starts the OLD executable. The user then lands back on the version they just
; updated away from, the app re-discovers the same update on its next feed check,
; and every subsequent attempt repeats it. Launching
; $INSTDIR\${APP_EXECUTABLE_FILENAME} names the bytes this installer just
; installed, which is the one target that cannot be stale -- and it is not an
; invented target: installSection.nsh assigns $launchLink exactly this path
; whenever no Start Menu shortcut exists, so this promotes upstream's own
; fallback to the only case. ${APP_EXECUTABLE_FILENAME} is defined globally in
; the template's common.nsh, so it resolves wherever this macro is inserted.
;
; Losing the shortcut's AppUserModelID with it costs nothing here: main.js calls
; app.setAppUserModelId() on win32 during startup, so taskbar grouping and
; notification identity are established by the app itself either way.
!macro customFinishPage
  !ifndef HIDE_RUN_AFTER_FINISH
    Function StartApp
      ${if} ${isUpdated}
        StrCpy $1 "--updated"
      ${else}
        StrCpy $1 ""
      ${endif}
      ${StdUtils.ExecShellAsUser} $0 "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "open" "$1"
    FunctionEnd

    !define MUI_FINISHPAGE_RUN
    !define MUI_FINISHPAGE_RUN_FUNCTION "StartApp"

    ; The extraction page is the entire update UI. Once it reaches 100%, start
    ; the updated app through electron-builder's locked launch contract and
    ; close successfully instead of waiting on a redundant Finish click.
    Function KiroFinishPagePre
      ${If} $KiroVisibleUpdate == 1
        ${If} ${isForceRun}
          Call StartApp
        ${EndIf}
        !insertmacro quitSuccess
      ${EndIf}
    FunctionEnd

    !define MUI_PAGE_CUSTOMFUNCTION_PRE KiroFinishPagePre
  !endif
  !define MUI_PAGE_CUSTOMFUNCTION_SHOW KiroFadeInPage
  !define MUI_PAGE_CUSTOMFUNCTION_LEAVE KiroFadeOutPage
  !insertmacro MUI_PAGE_FINISH
!macroend

; app-builder-lib stages the complete differential-aware 7z under $PLUGINSDIR,
; then normally copies every file into $INSTDIR. The bundled Python runtime is
; almost entirely under resources, so on the normal same-volume install path a
; directory rename publishes those thousands of files in one filesystem
; operation. Rename is attempted only for the two stable Electron directories
; in per-user installs. Per-machine installs deliberately use CopyFiles so the
; payload inherits the Program Files ACL instead of retaining the staging
; user's temporary-directory ACL. CopyFiles also handles root files, future
; directories, cross-volume TEMP layouts, occupied destinations and retries
; exactly as upstream does.
!macro customPublishAppPackage SOURCE DESTINATION
  ${If} $installMode != "all"
    ClearErrors
    Rename "${SOURCE}\resources" "${DESTINATION}\resources"
    ${If} ${Errors}
      ClearErrors
    ${EndIf}
    Rename "${SOURCE}\locales" "${DESTINATION}\locales"
    ${If} ${Errors}
      ClearErrors
    ${EndIf}
  ${EndIf}
  CopyFiles /SILENT "${SOURCE}\*" "${DESTINATION}"
!macroend

; Heal a PERSISTED shortcut left naming a stale sibling install root.
;
; The relaunch half of that staleness is already handled: StartApp launches
; $INSTDIR\${APP_EXECUTABLE_FILENAME} directly (see customFinishPage). What that
; cannot reach is the shortcut a user clicks by hand later: with
; KeepShortcuts="true", addStartMenuLink / addDesktopLink preserve whatever .lnk
; a previous install left behind, so on a machine carrying two sibling install
; roots both links can still name the stale one. Launching it lands on the
; frozen copy, which re-discovers the update and re-runs a full download +
; install on every launch.
;
; GATE. $keepShortcuts, not another FileExists probe, carries the ownership
; proof here: this macro expands after installApplicationFiles, where
; "$INSTDIR\${APP_EXECUTABLE_FILENAME}" exists unconditionally because this run
; just wrote it. installSection.nsh sets $keepShortcuts to "true" only after
; testing ${FileExists} "$appExe" BEFORE extraction -- the same
; executable-presence proof KiroEnsureAppInstallDir's update bypass requires --
; so ($KiroVisibleUpdate == 1) AND ($keepShortcuts == "true") is exactly "an
; update whose install root was proven ours pre-extraction, and whose shortcuts
; were preserved rather than recreated". A fresh install never reaches this,
; and the $keepShortcuts == "false" branch needs no healing: the template
; itself just re-created both links at $appExe there.
;
; REWRITE, NOT READ-AND-COMPARE. Reading a .lnk target needs the ShellLink
; plugin, which this build does not bundle (the template ships only WinShell /
; StdUtils / UAC), so an existing link is re-created at the root this run just
; wrote without reading its current target. CreateShortCut resets any
; arguments, icon or working directory a user edited onto a link, so the
; rewrite must never touch a healthy machine: it additionally requires
; PHYSICAL EVIDENCE OF BIFURCATION -- a sibling Kiro Crew install root in one
; of the two shapes the former collision-nesting produced (this app's
; executable directly in $INSTDIR's parent, or in an ${APP_FILENAME} child of
; $INSTDIR). $KiroStaleSibling defaults to 0 and only the two existence probes
; can set it, so a machine with a single install root keeps its customized
; shortcuts untouched on every update. On a bifurcated machine the rewrite
; discards customization on the two links it heals; a customized link naming a
; frozen copy is already broken, and that loss is the accepted cost of not
; bundling ShellLink. The ${FileExists} link gates keep a shortcut the user
; deleted deleted. Arguments mirror the template's own CreateShortCut calls
; ($appExe is "$INSTDIR\${APP_EXECUTABLE_FILENAME}"), and $newStartMenuLink /
; $newDesktopLink are the template's own resolved names (setLinkVars), so a
; template upgrade that renames a link moves this code with it.
;
; The AppUserModelID is re-stamped because CreateShortCut writes a fresh .lnk
; without one. This does not contradict customFinishPage's "costs nothing"
; note: main.js's app.setAppUserModelId() covers the running PROCESS identity,
; which is why the relaunch can name the executable directly, while the stamp
; here keeps the persisted .lnk itself carrying the id the shell groups and
; pins by. The two cover different surfaces; neither substitutes for the other.
;
; KNOWN UNCOVERED SURFACE: a taskbar PIN is the shell's own .lnk copy under
; the user's "User Pinned\TaskBar" tree, not either link rewritten here, so a
; pin created before the machine bifurcated keeps naming the stale root.
; Healing it would mean writing a literal shell path this template does not
; manage; that is a deliberate follow-up, not part of this change.
;
; A failed rewrite is surfaced, not swallowed: the details pane is muted on
; this path (installSection.nsh runs SetDetailsPrint none), and a silent
; failure here would mean the update reports success while the shortcut still
; names the stale root, with no trace to diagnose. The template's own
; unconditional ClearErrors covers a cosmetic "already exists" create; here
; the write IS the fix, so failure gets a breadcrumb first.
;
; THE STALE SIBLING DIRECTORY IS DELIBERATELY LEFT IN PLACE. Removing it would
; be a recursive delete under %LOCALAPPDATA%\Programs keyed off a path this run
; did not write; a wrong deletion there is unrecoverable for the user, and an
; ownership proof for a DELETE would have to be at least as strong as the one
; guarding the update bypass -- no such proof exists for the sibling. An
; unreferenced directory costs disk only.
!macro customInstall
  ; Default-deny: only the two probes below may arm the heal. Probe 1 catches
  ; a stale PARENT root (this update runs in the nested child); probe 2
  ; catches a stale NESTED child (this update runs in the parent). Both are
  ; the executable-presence test, the same evidence standard as the update
  ; bypass in KiroEnsureAppInstallDir.
  StrCpy $KiroStaleSibling 0
  ${GetParent} "$INSTDIR" $KiroStaleSiblingProbe
  ${If} $KiroStaleSiblingProbe != ""
  ${AndIf} $KiroStaleSiblingProbe != "$INSTDIR"
  ${AndIf} ${FileExists} "$KiroStaleSiblingProbe\${APP_EXECUTABLE_FILENAME}"
    StrCpy $KiroStaleSibling 1
  ${EndIf}
  ${If} ${FileExists} "$INSTDIR\${APP_FILENAME}\${APP_EXECUTABLE_FILENAME}"
    StrCpy $KiroStaleSibling 1
  ${EndIf}
  ${If} $KiroVisibleUpdate == 1
  ${AndIf} $keepShortcuts == "true"
  ${AndIf} $KiroStaleSibling == 1
    !ifndef DO_NOT_CREATE_START_MENU_SHORTCUT
      ${If} ${FileExists} "$newStartMenuLink"
        ClearErrors
        CreateShortCut "$newStartMenuLink" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" 0 "" "" "${APP_DESCRIPTION}"
        ${If} ${Errors}
          SetDetailsPrint both
          DetailPrint "Could not rewrite the Start Menu shortcut; it keeps its old target."
          SetDetailsPrint lastused
          ClearErrors
        ${EndIf}
        WinShell::SetLnkAUMI "$newStartMenuLink" "${APP_ID}"
      ${EndIf}
    !endif
    !ifndef DO_NOT_CREATE_DESKTOP_SHORTCUT
      ${If} ${FileExists} "$newDesktopLink"
        ClearErrors
        CreateShortCut "$newDesktopLink" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" "" "$INSTDIR\${APP_EXECUTABLE_FILENAME}" 0 "" "" "${APP_DESCRIPTION}"
        ${If} ${Errors}
          SetDetailsPrint both
          DetailPrint "Could not rewrite the Desktop shortcut; it keeps its old target."
          SetDetailsPrint lastused
          ClearErrors
        ${EndIf}
        WinShell::SetLnkAUMI "$newDesktopLink" "${APP_ID}"
      ${EndIf}
    !endif
  ${EndIf}
!macroend

; Preserve only the install-root ownership guard from the former custom UI.
; This runs for silent installs too and does not replace or restyle any page.
!macro customInit
  StrCpy $KiroWindowVisible 0
  ; Resolved HERE, not at the use site: see the Var declaration for why a
  ; Function body cannot reference ${APP_EXECUTABLE_FILENAME} directly.
  StrCpy $KiroAppExeName "${APP_EXECUTABLE_FILENAME}"
  StrCpy $KiroVisibleUpdate 0
  ${If} ${isUpdated}
    StrCpy $KiroVisibleUpdate 1
    ; Older Kiro Crew clients launch every NSIS update with /S. Convert that
    ; legacy handoff to the visible, non-interactive update path so users see
    ; progress on the very first upgrade that contains this fix.
    ${If} ${Silent}
      SetSilent normal
    ${EndIf}
  ${EndIf}
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
