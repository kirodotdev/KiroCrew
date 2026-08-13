; Custom NSIS include, auto-discovered by electron-builder as
; <buildResourcesDir>/installer.nsh (NsisTarget.computeCommonInstallerScriptHeader
; resolves it via packager.getResource). Its macros are inserted into the
; generated script; nothing here runs unless the macro name matches a hook the
; template inserts.
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
    DetailPrint "Removing update cache: $LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
    RMDir /r "$LOCALAPPDATA\${APP_PACKAGE_NAME}-updater"
  ${endIf}
!macroend
