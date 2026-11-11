import { useEffect, useRef } from 'react'
import { useBranding } from '../hooks/useBranding'
import { getThemeBranding } from '../themeBranding'
import { instanceFavicon } from '../lib/instanceFavicon'

/**
 * The shell's product identity: the label, the rail logo and the favicon, resolved
 * from compiled edition branding, then an installed theme, then the configured
 * product branding; plus the active theme's activation side effect.
 */
export function useShellBranding({ colorTheme, themeVersion, brandName, brandLogo, brandFavicon }: {
  colorTheme: string
  /** Bumped after every theme apply: the favicon re-reads the accent on it. */
  themeVersion: number
  brandName: string | null
  brandLogo: string | null
  brandFavicon: string | null
}) {
  const { botName: _botName, avatar: _avatar, instanceLabel } = useBranding()

  // Compiled edition branding wins when registered. Otherwise an active
  // installed theme may supply the shell label, left-rail logo, and favicon;
  // configured product branding remains the final fallback. A labelled
  // instance's generated icon comes just before the stock one.
  const branding = getThemeBranding(colorTheme)
  const botName = branding?.botName ?? brandName ?? _botName
  const avatar = branding?.logo ?? brandLogo ?? _avatar
  useEffect(() => {
    const link = document.querySelector<HTMLLinkElement>('link[rel~="icon"]')
    if (!link) return
    const themed = branding?.favicon ?? brandFavicon
    if (themed || !instanceLabel) {
      link.href = themed ?? '/logo.png'
      return
    }
    const css = getComputedStyle(document.documentElement)
    link.href = instanceFavicon(instanceLabel, css.getPropertyValue('--accent').trim(), css.getPropertyValue('--accent-fg').trim())
  }, [branding, brandFavicon, instanceLabel, themeVersion])
  // Fire a theme's activation side-effect (e.g. a boot chime) on each off→on
  // switch to that theme. Generic via the branding registry; the effect itself
  // is owned by the theme's registration, so the core stays silent by default.
  const prevColorThemeRef = useRef<string | null>(null)
  useEffect(() => {
    if (colorTheme !== prevColorThemeRef.current) {
      prevColorThemeRef.current = colorTheme
      // Guarded: a registered theme's activation side-effect (owned by the
      // downstream edition) must not crash the effect / shell if it throws.
      try {
        branding?.onActivate?.()
      } catch (err) {
        // eslint-disable-next-line no-console
        console.error('[themeBranding] onActivate threw', err)
      }
    }
  }, [colorTheme]) // eslint-disable-line react-hooks/exhaustive-deps
  return { branding, botName, avatar, instanceLabel }
}
