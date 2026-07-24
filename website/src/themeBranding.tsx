/**
 * Per-theme branding registry.
 *
 * A theme can decorate the shell beyond its color tokens: a custom bot name and
 * logo, a browser favicon, a decorative top-bar element, an aside widget,
 * always-mounted overlays, and a one-shot activation side-effect. Rather than
 * hard-coding `colorTheme === 'x' ? … : colorTheme === 'y' ? …` chains in
 * App.tsx and WelcomeView, those components read this registry, so adding a
 * branded theme is ONE `registerThemeBranding()` call (plus the theme's CSS
 * block in index.css and any referenced assets) — no component edits.
 *
 * This is also the extension seam for a downstream edition: it ships its own
 * theme components and registers them from the extensions.ts composition root instead of editing
 * App.tsx on every upstream sync. The core ships only the branding for themes
 * it bundles; with none registered the shell renders its default chrome and
 * every slot below is simply absent.
 *
 * Scope: registration is expected at module-load time (edition composition),
 * before App mounts — this registry is not reactive, so registering after the
 * shell has rendered will not take effect until the next theme switch or an
 * unrelated re-render.
 */
import type { ComponentType } from 'react'
import { reportSeamCollision } from './apps/seamCollision'

export interface ThemeBranding {
  /** Overrides the dashboard bot name (e.g. 'LumonClaw'). */
  botName?: string
  /** Top-bar logo / avatar image path. */
  logo?: string
  /** Tailwind sizing classes for the top-bar logo <img> (default 'w-10 h-10'). */
  logoClass?: string
  /** Browser favicon path. Omit to keep the default '/logo.png'. */
  favicon?: string
  /** Decorative element in the center top-bar slot, chosen by resolved mode.
   *  (Themes that aren't mode-dependent set dark and light to the same one.) */
  topBar?: { dark?: ComponentType; light?: ComponentType }
  /** Extra decorative element rendered in the right-hand top-bar controls. */
  topBarAside?: ComponentType
  /** Hide topBar / topBarAside on narrow (mobile) viewports. Default false. */
  topBarHideOnMobile?: boolean
  /** Always-mounted decorative overlays (widgets, transitions). */
  overlays?: ComponentType[]
  /** Side-effect fired once when this theme becomes active (off→on switch),
   *  e.g. a boot chime. Must be idempotent / cheap. */
  onActivate?: () => void
}

/**
 * Registry mapping a color-theme slug to its branding. The core seeds the
 * themes it bundles; downstream bundles extend it via `registerThemeBranding()`.
 */
const THEME_BRANDING: Record<string, ThemeBranding> = {
  lumon: {
    botName: 'LumonClaw',
    logo: '/static/lumon-logo.png',
    logoClass: 'w-auto h-10',
  },
}

/**
 * Register branding for one or more theme slugs at runtime. Duplicate slugs are
 * ignored (core registrations win) and log a warning.
 */
export function registerThemeBranding(entries: Record<string, ThemeBranding>): void {
  for (const [slug, branding] of Object.entries(entries)) {
    if (slug in THEME_BRANDING) {
      reportSeamCollision('themeBranding', `theme ${slug} already registered; ignoring duplicate`)
      continue
    }
    THEME_BRANDING[slug] = branding
  }
}

/** Resolve a theme slug to its branding, or undefined when it has none. */
export function getThemeBranding(slug: string): ThemeBranding | undefined {
  return THEME_BRANDING[slug]
}
