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

  return { paletteColors, boost, isDark, colorMode, paletteName, intensity }
}
