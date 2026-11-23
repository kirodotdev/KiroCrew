import { useState, useLayoutEffect, useMemo, useRef } from 'react'
import { useTheme } from './useTheme'
import { useAppSelector } from '../store'
import { generatePalette, computePaletteBoost } from '../utils/sessionColors'
import type { SessionColorMode, PaletteName, IntensityName, PaletteBoost } from '../utils/sessionColors'

function readVars() {
  const cs = getComputedStyle(document.documentElement)
  return { accentSubtle: cs.getPropertyValue('--accent-subtle').trim(), accent: cs.getPropertyValue('--accent').trim(), bgAccent: cs.getPropertyValue('--bg-accent').trim(), muted: cs.getPropertyValue('--muted').trim(), text: cs.getPropertyValue('--text').trim(), textStrong: cs.getPropertyValue('--text-strong').trim() }
}
type SessionVars = ReturnType<typeof readVars>
const sameVars = (a: SessionVars, b: SessionVars) =>
  (Object.keys(a) as (keyof SessionVars)[]).every(k => a[k] === b[k])

/** Shared hook: reads CSS accent vars, generates palettes, computes per-color boost. */
export function useSessionPalette() {
  const { theme: themeMode, colorTheme, themeVersion } = useTheme()
  const isDark = themeMode === 'dark'

  const [vars, setVars] = useState(readVars)
  const initialVars = useRef<SessionVars | null>(vars)

  // themeVersion is the re-read trigger. React fires useEffect child-first, so
  // without themeVersion in the deps the first read would land while
  // <html data-theme> is still unset and all CSS vars resolve to ''.
  // themeVersion bumps on every applyTheme / loadCustomThemes, which covers
  // both the initial mount and any later theme / custom-theme change.
  // On mount the read in useState above already resolved the theme, so the
  // effect does not read again: in a layout effect a read forces a style and
  // layout pass over the page that just mounted, inside the task that mounts
  // it. A read that changes nothing keeps the old object, so it re-renders
  // nothing.
  useLayoutEffect(() => {
    const first = initialVars.current
    initialVars.current = null
    if (first && (first.accent || first.accentSubtle)) return
    const next = readVars()
    setVars(prev => (sameVars(prev, next) ? prev : next))
  }, [themeMode, colorTheme, themeVersion])

  const colorMode = useAppSelector(s => s.dashboard.sessionColorsMode) as SessionColorMode
  const paletteName = useAppSelector(s => s.dashboard.sessionColorsPalette) as PaletteName
  const intensity = useAppSelector(s => s.dashboard.sessionColorsIntensity) as IntensityName

  const seed = vars.accentSubtle || vars.accent

  const paletteColors = useMemo(
    () => generatePalette(seed, paletteName, vars.bgAccent),
    [seed, paletteName, vars.bgAccent],
  )

  const boost = useMemo<PaletteBoost>(
    () => computePaletteBoost(paletteColors, vars.bgAccent, vars.muted, vars.text, isDark, intensity, vars.textStrong),
    [paletteColors, vars.bgAccent, vars.muted, vars.text, vars.textStrong, isDark, intensity],
  )

  /** Per-hex boost for CUSTOM session colors (color_hex), memoized across
   *  rows: a custom hex bypasses the generated palette but must still get the
   *  same APCA muted-text adaptation, or body text over the tinted row goes
   *  illegible. The cache resets whenever theme vars or intensity change
   *  (the useMemo deps), so entries never go stale. */
  const boostFor = useMemo(() => {
    const cache = new Map<string, PaletteBoost>()
    return (hex: string): PaletteBoost => {
      let b = cache.get(hex)
      if (!b) {
        b = computePaletteBoost([hex], vars.bgAccent, vars.muted, vars.text, isDark, intensity, vars.textStrong)
        cache.set(hex, b)
      }
      return b
    }
  }, [vars.bgAccent, vars.muted, vars.text, vars.textStrong, isDark, intensity])

  return { paletteColors, boost, boostFor, isDark, colorMode, paletteName, intensity }
}
