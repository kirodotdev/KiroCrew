import { useEffect, useState } from 'react'
import { isMacElectron } from '../../lib/electron'

/**
 * The shell's calls into the Electron preload bridge (`window.electronAPI`).
 * Every one reads the bridge at call time, so a browser tab (no bridge) and a
 * test that installs one after the shell's modules load both see the current
 * value; each is a no-op when the bridge or the method is absent.
 */

/** Native window chrome (traffic lights, the injected drag bar) follows focus mode's header. */
export function setNativeFocusChrome(visible: boolean): void {
  const api = window.electronAPI
  api?.setFocusModeChrome?.(visible)
}

/** The dock / taskbar badge mirrors the bell's unread attention count. */
export function setNativeBadgeCount(count: number): void {
  const api = window.electronAPI
  api?.setBadgeCount?.(count)
}

/** The View > DevTools menu follows developer mode. */
export function setNativeDevMode(on: boolean): void {
  const electronAPI = window.electronAPI
  electronAPI?.setDevMode?.(on)
}

/**
 * In-app paths the main process sends (native app-menu items, the Crew
 * Companion's "Open session"). Accept only plain absolute app paths — rejects
 * protocol-relative ("//host") and external URLs by construction. Returns the
 * unsubscribe, or nothing outside the desktop app.
 */
export function subscribeNativeNavigate(onPath: (path: string) => void): (() => void) | undefined {
  const electronAPI = window.electronAPI
  if (!electronAPI?.onNavigate) return
  return electronAPI.onNavigate(path => {
    if (typeof path !== 'string' || !/^\/(?!\/)/.test(path)) return
    onPath(path)
  })
}

/**
 * macOS fullscreen hides the native traffic lights, so the header's 84px
 * clearance inset drops while fullscreen (mac-fullscreen class on the root).
 * Native traffic lights sit over the consolidated 42px header, so there is no
 * separate strip inset to relay to Electron — positionTrafficLights centers on
 * the header height directly. Remote panes get their own inset via `macInset`.
 */
export function useMacFullscreen() {
  const [macFullscreen, setMacFullscreen] = useState(false)
  useEffect(() => {
    if (!isMacElectron) return
    const api = (window as { electronAPI?: { onFullScreenChanged?: (cb: (fs: boolean) => void) => () => void } }).electronAPI
    return api?.onFullScreenChanged?.(setMacFullscreen)
  }, [])
  const macInset = isMacElectron && !macFullscreen
  return { macFullscreen, macInset }
}
