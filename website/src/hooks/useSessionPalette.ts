import { useState, useLayoutEffect, useMemo } from 'react'
import { useTheme } from './useTheme'
import { useAppSelector } from '../store'
import { generatePalette, computePaletteBoost } from '../utils/sessionColors'
import type { SessionColorMode, PaletteName, IntensityName, PaletteBoost } from '../utils/sessionColors'

function readVars() {
  const cs = getComputedStyle(document.documentElement)
  return { accentSubtle: cs.getPropertyValue('--accent-subtle').trim(), accent: cs.getPropertyValue('--accent').trim(), bgAccent: cs.getPropertyValue('--bg-accent').trim(), muted: cs.getPropertyValue('--muted').trim(), text: cs.getPropertyValue('--text').trim(), textStrong: cs.getPropertyValue('--text-strong').trim() }
}

/** Shared hook: reads CSS accent vars, generates palettes, computes per-color boost. */
export function useSessionPalette() {
  const { theme: themeMode, colorTheme, themeVersion } = useTheme()
  const isDark = themeMode === 'dark'

  const [vars, setVars] = useState(readVars)

  // themeVersion is the re-read trigger. React fires useEffect child-first, so
  // without themeVersion in the deps the first read would land while
  // <html data-theme> is still unset and all CSS vars resolve to ''.
  // themeVersion bumps on every applyTheme / loadCustomThemes, which covers
  // both the initial mount and any later theme / custom-theme change.
  useLayoutEffect(() => { setVars(readVars()) }, [themeMode, colorTheme, themeVersion])

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
