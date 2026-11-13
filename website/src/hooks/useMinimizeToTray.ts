import { useMainWindowSetting } from './useMainWindowSetting'

// Minimize-to-tray bridge exposed by electron/preload.js.
const minimizeToTrayAPI = (): MinimizeToTrayAPI | undefined => window.minimizeToTrayAPI

// Only an exact true counts, so an unexpected answer reads as not offered and off.
const canonical = (v: unknown): MinimizeToTrayState => {
  const s = (v ?? {}) as Partial<MinimizeToTrayState>
  return { on: s.on === true, offered: s.offered === true, canEnable: s.canEnable === true }
}

const INITIAL: MinimizeToTrayState = { on: false, offered: false, canEnable: false }

/**
 * Read/write whether minimizing the desktop app hides it to the tray.
 *
 * It can be turned on only while the tray icon shows, so the state is read
 * again whenever `trayIconShown` changes.
 */
export function useMinimizeToTray(trayIconShown: boolean) {
  const s = useMainWindowSetting(minimizeToTrayAPI, canonical, INITIAL, trayIconShown)
  return {
    minimizeToTray: s.value,
    minimizeToTraySupported: s.supported,
    minimizeToTrayPending: s.pending,
    minimizeToTrayFailed: s.failed,
    setMinimizeToTray: s.set,
  }
}
