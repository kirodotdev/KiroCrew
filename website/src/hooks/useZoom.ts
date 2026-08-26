import { useState, useEffect, useCallback } from 'react'
import { safeSetItem } from '../utils/safeStorage'
import { OPENDYSLEXIC_BODY_STACK, OPENDYSLEXIC_MONO_STACK } from '../utils/fontFamilyOptions'

export type FontFamily = 'sans' | 'mono' | 'system' | 'opendyslexic'

const FAMILIES: FontFamily[] = ['sans', 'mono', 'system', 'opendyslexic']
// The two theme-able options read a role token an installed pack can fill, so a
// pack's proportional face reaches Sans and its monospace face reaches Mono. An
// unfilled token falls through to Kiro Crew's own stack, which is what leaves a
// colour-only pack (or a pack that ships just one role) on the built-in families.
// System deliberately reads no token: the OS face is the one choice a theme must
// never be able to take away.
// OpenDyslexic is bundled (see index.css @font-face) and deliberately does NOT
// read a theme token — an accessibility choice must not be hijackable by a pack
// declaring its own proportional face. The CSS stack itself lives in
// utils/fontFamilyOptions.ts so the ESLint i18n exemption covers only that one
// file rather than this hook.
const FAMILY_MAP: Record<FontFamily, string> = {
  sans: "var(--theme-font-sans, var(--script-fallbacks),'Space Grotesk',-apple-system,BlinkMacSystemFont,sans-serif)",
  mono: "var(--theme-font-mono, var(--script-fallbacks-mono),'JetBrains Mono',ui-monospace,SFMono-Regular,monospace)",
  system: "var(--script-fallbacks),-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
  opendyslexic: OPENDYSLEXIC_BODY_STACK,
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
    //
    // OpenDyslexic also overrides --mono to OpenDyslexicMono: when selected,
    // the --mono token is inline-set on <html> so code blocks and diffs pick
    // it up. Leaving that family removes the inline override so the CSS :root
    // default (JetBrains Mono) takes over again. Written as an explicit
    // `family === 'opendyslexic'` check rather than a lookup table because
    // OpenDyslexic is the only family that does this today — a future a11y
    // font becomes a second condition in the same branch.
    const html = document.documentElement
    const apply = () => {
      const ui = html.dataset.ui
      // Auto-resolve to mono in CLI mode for the default family ('sans').
      // OpenDyslexic in CLI mode also flips its body to the mono variant, using
      // its own OpenDyslexicMono face rather than falling into JetBrains Mono.
      // Explicit 'mono' / 'system' choices are always honoured as-is.
      const isDefaultFamily = family === 'sans'
      const cliDefaultAutoMono = ui === 'cli' && isDefaultFamily
      const cliOpenDyslexicMono = ui === 'cli' && family === 'opendyslexic'

      let bodyStack: string
      if (cliDefaultAutoMono) {
        bodyStack = FAMILY_MAP.mono
      } else if (cliOpenDyslexicMono) {
        bodyStack = OPENDYSLEXIC_MONO_STACK
      } else {
        bodyStack = FAMILY_MAP[family]
      }
      html.style.setProperty('--font-body', bodyStack)

      // Publish the RESOLVED family as a data attribute so CSS can react to it.
      // The sans→mono auto-resolve reports "mono" so the JetBrains-Mono-tuned
      // rail letter-spacing rule in index.css fires. OpenDyslexic in CLI mode
      // stays as "opendyslexic" — that rule is calibrated for JetBrains Mono's
      // narrower glyphs and would harm OpenDyslexicMono's wider, dyslexia-
      // friendly design if inherited.
      const effectiveDataAttr: FontFamily = cliDefaultAutoMono ? 'mono' : family
      html.dataset.fontFamily = effectiveDataAttr

      // Apply or clear the --mono inline override. Only opendyslexic overrides
      // today, so the branch is explicit rather than table-driven — a future
      // a11y font with its own mono variant becomes a second `||` in the
      // condition. Removing the inline value on switch-away lets the CSS :root
      // default (JetBrains Mono) take over again.
      if (family === 'opendyslexic') {
        html.style.setProperty('--mono', OPENDYSLEXIC_MONO_STACK)
      } else {
        html.style.removeProperty('--mono')
      }
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
