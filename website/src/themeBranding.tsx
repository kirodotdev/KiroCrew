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
  /**
   * Replace the ENTIRE chat loading indicator (the animation in the footer while
   * a turn runs) with the theme's own component. Use this when the theme wants
   * something other than the stock 4-slot cross-fading carousel — a mascot
   * animation, a progress bar, a spinner, a canvas, anything.
   *
   * Rendered in place of the default loader, with no wrapper of its own beyond the
   * footer's padding, so the component owns its size, layout and motion. It should
   * stay small (the footer band is ~32px tall), be purely decorative
   * (`aria-hidden`), and honour `prefers-reduced-motion` itself.
   *
   * Takes precedence over `loaderIcons`. For the common case of "same carousel,
   * different icons", prefer `loaderIcons` — it inherits the cross-fade, the
   * cascade timing and the reduced-motion handling for free.
   */
  loader?: ComponentType
  /**
   * Artwork for the DEFAULT loading carousel (the four cross-fading icons in the
   * footer while a turn runs). Supply at least 4; the carousel shows 4 at a time
   * and re-samples a distinct set from this pool each beat, so a longer list
   * gives more variety. Omit to inherit the default icon set.
   *
   * Each entry renders its own `<svg>` and needs no props — the carousel sizes it
   * (14px) and the icon inherits `currentColor` (the accent) unless the theme's
   * own CSS block says otherwise, so a theme can ship flat silhouettes or
   * multi-fill artwork. Colour/stroke belong in the theme's CSS, scoped the usual
   * way (e.g. `[data-theme="mytheme-light"] .csb4 …`), NOT in the components.
   *
   * Ignored when `loader` is set.
   */
  loaderIcons?: ComponentType[]
}

/**
 * Registry mapping a color-theme slug to its branding. The core ships no
 * seeded registrations (installed theme packs carry their own branding);
 * downstream bundles extend it via `registerThemeBranding()`.
 */
const THEME_BRANDING: Record<string, ThemeBranding> = {}

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
