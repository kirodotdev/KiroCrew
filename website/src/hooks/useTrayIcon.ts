import { useMainWindowSetting } from './useMainWindowSetting'

// Tray-icon bridge exposed by electron/preload.js.
const trayIconAPI = (): TrayIconAPI | undefined => window.trayIconAPI

// Only an exact false hides the icon and only an exact true unlocks the switch,
// so an unexpected answer reads as an untouched install.
const canonical = (v: unknown): TrayIconState => {
  const s = (v ?? {}) as Partial<TrayIconState>
  return { show: s.show !== false, canHide: s.canHide === true }
}

/**
 * Read/write whether the desktop app shows its tray icon.
 *
 * Whether the icon may be hidden depends on what the close button does, so the
 * state is read again whenever `closeAction` changes.
 */
export function useTrayIcon(closeAction: CloseAction) {
  const s = useMainWindowSetting(trayIconAPI, canonical, { show: true, canHide: false }, closeAction)
  return {
    trayIcon: s.value,
    trayIconSupported: s.supported,
    trayIconPending: s.pending,
    trayIconFailed: s.failed,
    setTrayIcon: s.set,
  }
}
