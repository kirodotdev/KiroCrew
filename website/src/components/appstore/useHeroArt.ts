/**
 * useHeroArt — theme-aware hero image resolution for store surfaces.
 *
 * Resolution order: prefer the current theme's artwork, fall
 * back to the opposite theme, then the first screenshot. Callers pair the
 * returned ``src`` with ``failed``/``onError`` so a 404'd hero degrades to the
 * gradient instead of rendering a blank panel.
 */
import { useEffect, useState } from 'react'
import { useTheme } from '../../hooks/useTheme'
import type { RegistryApp } from './types'

type HeroFields = Pick<RegistryApp, 'heroImage' | 'heroImageDark' | 'screenshots'>

/**
 * True when the app ships ANY art ``useHeroArt`` could render (either theme's
 * hero, or a screenshot). Featured ranking uses this so a dark-only or
 * screenshot-only app is not treated as art-less.
 */
export function hasHeroArt(app: HeroFields): boolean {
  return !!(app.heroImage || app.heroImageDark || app.screenshots?.[0])
}

export function useHeroArt(app: HeroFields): { src: string; onError: () => void } {
  const { theme } = useTheme()
  const dark = theme === 'dark'
  const resolved = (dark
    ? (app.heroImageDark || app.heroImage)
    : (app.heroImage || app.heroImageDark)) || app.screenshots?.[0] || ''
  const [failed, setFailed] = useState('')
  // Reset the failure latch when the resolved art changes (theme flip, or a
  // re-fetch that filled in metadata) so a new URL gets a fresh attempt.
  useEffect(() => { setFailed('') }, [resolved])
  return {
    src: failed === resolved ? '' : resolved,
    onError: () => setFailed(resolved),
  }
}
