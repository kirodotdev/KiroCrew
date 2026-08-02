import { useState, useEffect, useCallback } from 'react'
import { safeSetItem } from '../utils/safeStorage'

export type FontFamily = 'sans' | 'mono' | 'system'

const FAMILIES: FontFamily[] = ['sans', 'mono', 'system']
const FAMILY_MAP: Record<FontFamily, string> = {
  sans: "var(--script-fallbacks),'Space Grotesk',-apple-system,BlinkMacSystemFont,sans-serif",
  mono: "var(--script-fallbacks-mono),'JetBrains Mono',ui-monospace,SFMono-Regular,monospace",
  system: "var(--script-fallbacks),-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
}

// Native zoom bridge exposed by electron/preload.js. Chromium's per-origin
// zoom (what Cmd/Ctrl +/- changes) is the ONLY zoom mechanism in KiroCrew:
// the desktop app exposes read/write access to it over IPC, and Chromium
// itself persists the factor per-origin across launches. In a plain browser
// the bridge is absent — a web page cannot drive the browser's native zoom —
// so the UI falls back to a keyboard-shortcut hint (`zoomSupported: false`).
type ZoomAPI = {
  get(): Promise<number>
  set(factor: number): Promise<number>
  step(dir: 1 | -1): Promise<number>
}
const zoomAPI = (): ZoomAPI | undefined => (window as { zoomAPI?: ZoomAPI }).zoomAPI

// Legacy page-side scaling (removed): a CSS `zoom` on #root ('mc-zoom') and an
// html font-size scale ('mc-font-scale') that stacked with native zoom into
// three multiplying mechanisms. One-time migration: fold the combined legacy
// scale into the native zoom factor (desktop only — browsers can't be set
// programmatically), then drop the keys so page scaling never re-applies.
const LEGACY_ZOOM_KEY = 'mc-zoom'
const LEGACY_FONT_SCALE_KEY = 'mc-font-scale'
function migrateLegacyScale(api: ZoomAPI | undefined) {
  const zoomRaw = localStorage.getItem(LEGACY_ZOOM_KEY)
  const fontRaw = localStorage.getItem(LEGACY_FONT_SCALE_KEY)
  if (zoomRaw === null && fontRaw === null) return
  localStorage.removeItem(LEGACY_ZOOM_KEY)
  localStorage.removeItem(LEGACY_FONT_SCALE_KEY)
  if (!api) return
  const zoom = parseInt(zoomRaw || '100', 10)
  const font = parseInt(fontRaw || '100', 10)
  if (Number.isNaN(zoom) || Number.isNaN(font)) return
  const combined = (zoom / 100) * (font / 100)
  if (Math.abs(combined - 1) < 0.005) return
  // Same bounds as electron/zoom.js (main clamps again regardless).
  void api.set(Math.min(3, Math.max(0.5, combined))).catch(() => {})
}

export function useZoom() {
  // Percent view of the native zoom factor (100 = 1.0). In browsers this
  // stays 100 and zoomSupported is false — the value is never shown there.
  const [zoom, setZoomPct] = useState(100)
  const zoomSupported = !!zoomAPI()
  const [family, setFamily] = useState<FontFamily>(
    () => (localStorage.getItem('mc-font-family') as FontFamily) || 'sans'
  )

  useEffect(() => {
    const api = zoomAPI()
    migrateLegacyScale(api)
    if (!api) return
    let alive = true
    const sync = () => {
      void api.get().then(f => { if (alive) setZoomPct(Math.round(f * 100)) }).catch(() => {})
    }
    sync()
    // Native zoom changes from OUTSIDE this hook (View menu Cmd/Ctrl +/-,
    // ctrl+wheel) resize the CSS-pixel viewport, which fires window 'resize'.
    // Re-reading on resize keeps the Settings stepper live without a push
    // channel; plain window resizes just re-read an unchanged value.
    window.addEventListener('resize', sync)
    return () => { alive = false; window.removeEventListener('resize', sync) }
  }, [])

  const applyResult = useCallback((p: Promise<number>) => {
    void p.then(f => setZoomPct(Math.round(f * 100))).catch(() => {})
  }, [])
  const zoomIn = useCallback(() => {
    const api = zoomAPI()
    if (api) applyResult(api.step(1))
  }, [applyResult])
  const zoomOut = useCallback(() => {
    const api = zoomAPI()
    if (api) applyResult(api.step(-1))
  }, [applyResult])
  const reset = useCallback(() => {
    const api = zoomAPI()
    if (api) applyResult(api.set(1))
  }, [applyResult])

  useEffect(() => {
    // Apply --font-body from the user's Font Family preference, with one
    // exception: when the dashboard is in CLI mode (data-ui="cli") AND the
    // user is on the default 'sans' (i.e. has never explicitly picked a
    // family), resolve to 'mono' so the CLI surface looks monospace by
    // default. If the user explicitly picks Mono / Sans / System, that
    // choice is honoured everywhere — including CLI mode.
    const html = document.documentElement
    const apply = () => {
      const ui = html.dataset.ui
      // Auto-resolve to mono in CLI mode for the default family ('sans').
      // Explicit 'mono' / 'system' choices are always honoured.
      const isDefaultFamily = family === 'sans'
      const effective: FontFamily =
        (ui === 'cli' && isDefaultFamily) ? 'mono' : family
      html.style.setProperty('--font-body', FAMILY_MAP[effective])
      // Publish the RESOLVED family (after the CLI-mode override) as a data
      // attribute so CSS can react to it. Needed because some tight chrome has
      // to compensate for how much wider JetBrains Mono is than Space Grotesk —
      // see the `[data-font-family="mono"]` rule in index.css. Reads the
      // effective value, not `family`, so CLI mode's auto-mono is covered too.
      html.dataset.fontFamily = effective
    }
    apply()
    // Re-apply on data-ui changes (e.g. user toggles Interface in Settings).
    const obs = new MutationObserver(apply)
    obs.observe(html, { attributes: true, attributeFilter: ['data-ui'] })
    return () => obs.disconnect()
  }, [family])

  const setFontFamily = useCallback((f: FontFamily) => {
    safeSetItem('mc-font-family', f)
    setFamily(f)
  }, [])

  const cycleFamily = useCallback(() => {
    const next = FAMILIES[(FAMILIES.indexOf(family) + 1) % FAMILIES.length]
    safeSetItem('mc-font-family', next)
    setFamily(next)
  }, [family])

  return { zoom, zoomSupported, zoomIn, zoomOut, reset, family, setFontFamily, cycleFamily }
}
