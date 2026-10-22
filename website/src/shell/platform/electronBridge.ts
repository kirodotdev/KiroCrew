import { useEffect, useState } from 'react'
import { isMacElectron, MAC_FULLSCREEN_TOP_RESERVE_PX } from '../../lib/electron'

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
 * The native View > Toggle Focus Mode item. Returns the unsubscribe, or nothing
 * outside the desktop app.
 */
export function subscribeNativeFocusModeToggle(onToggle: () => void): (() => void) | undefined {
  const electronAPI = window.electronAPI
  return electronAPI?.onToggleFocusMode?.(onToggle)
}

/**
 * One route-history step the main process relays from a macOS swipe (mouse
 * software such as Logitech's Options sends Back/Forward as swipes).
 * Accepts only -1 (Back) and 1 (Forward). Returns the unsubscribe, or nothing
 * outside the desktop app.
 */
export function subscribeNativeHistoryStep(onStep: (delta: -1 | 1) => void): (() => void) | undefined {
  const electronAPI = window.electronAPI
  if (!electronAPI?.onHistoryStep) return
  return electronAPI.onHistoryStep(delta => {
    if (delta === -1 || delta === 1) onStep(delta)
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
  // On a notched built-in display macOS places the fullscreen window below the
  // menu bar, so the menu bar never covers the header and no band is kept.
  // The shell says so per event; a shell that does not keeps the band.
  const [menuBarOverlap, setMenuBarOverlap] = useState(true)
  const [zoomFactor, setZoomFactor] = useState(1)
  useEffect(() => {
    if (!isMacElectron) return
    const api = (window as { electronAPI?: { onFullScreenChanged?: (cb: (fs: boolean, menuBarOverlap?: boolean) => void) => () => void } }).electronAPI
    return api?.onFullScreenChanged?.((fs, overlap) => {
      setMacFullscreen(fs)
      setMenuBarOverlap(overlap !== false)
    })
  }, [])
  // The strip AppKit keeps at the top of a fullscreen window is measured in
  // screen points, while the reserve is laid out in CSS px, which native zoom
  // scales. A zoom change resizes the CSS viewport, so re-read on 'resize'.
  useEffect(() => {
    const zoom = window.zoomAPI
    if (!macFullscreen || !zoom) return
    let alive = true
    const sync = () => {
      void zoom.get().then(f => { if (alive && f > 0) setZoomFactor(f) }).catch(() => {})
    }
    sync()
    window.addEventListener('resize', sync)
    return () => { alive = false; window.removeEventListener('resize', sync) }
  }, [macFullscreen])
  const macInset = isMacElectron && !macFullscreen
  // CSS px kept clear above the header, so the header sits below that strip.
  const topReservePx = macFullscreen && menuBarOverlap
    ? Math.round(MAC_FULLSCREEN_TOP_RESERVE_PX / zoomFactor)
    : 0
  return { macFullscreen, macInset, topReservePx }
}
