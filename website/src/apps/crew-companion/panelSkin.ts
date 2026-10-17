/**
 * Panel palette — derived from the Kiro Crew theme the user actually picked.
 *
 * This file previously held two hand-written palettes (a cream light one and a dark
 * one) selected by `mode`. That was wrong: `src/shared/themes.ts` already links the
 * dashboard's own stylesheet, sets `data-theme`, and aliases this app's older
 * variable names onto Kiro Crew's — so every other window follows all ~36 themes,
 * and only the panel ignored the user's colour choice.
 *
 * So the values here are CSS custom properties, not hex. Each carries the
 * `kiro-dark` fallback as its default, which matters twice: before the dashboard
 * stylesheet loads, and permanently if the gateway is unreachable.
 *
 * TRADEOFF, stated plainly: following an arbitrary user theme means the panel's
 * contrast can no longer be proven at build time the way two fixed palettes could.
 * The fallback palette IS still asserted (see panelContrast.test.ts); the other 35
 * themes are the dashboard's own designed palettes, and we inherit their choices
 * rather than second-guessing them per surface.
 */

import { createContext, useContext } from 'react'
import { pickReadable } from './contrast'

/**
 * The pet's typeface. Rounded rather than the system default: the panel is a
 * companion surface, not a settings dialog. Shared so the avatar collection matches.
 */
export const PANEL_FONT = 'var(--cc-panel-font)'

/** Shape language shared by the panel and the avatar collection. */
export const PANEL_RADIUS = { card: 16, row: 11, pill: 999, thumb: 11 } as const

export interface PanelSkin {
  /** Card background. */
  card: string
  /** Primary text. */
  ink: string
  /** Secondary text. */
  muted: string
  /**
   * Least prominent text (footer, placeholder).
   *
   * Deliberately the SAME token as `muted`. The app's own vocabulary has a third
   * `--text-faint` tier, but a distinct faint tier is exactly where this panel's
   * contrast previously failed WCAG AA (measured 2.62:1 on the footer). The panel
   * carries two text tiers instead of three rather than reintroducing that.
   */
  faint: string
  /** Inset row background. */
  row: string
  /** Divider. */
  hairline: string
  /** Accent, for FILLS (button backgrounds, pills) where contrast is on `onAccent`. */
  accent: string
  /**
   * Accent for TEXT (labels, links) — resolved at runtime.
   *
   * Separate from `accent` because a theme accent that looks right as a button fill
   * is often unreadable as small text: the fallback theme's accent measures 3.57:1
   * on the card, below AA. `resolveAccentText()` publishes --cc-accent-text as
   * either the accent or the primary text colour, whichever is legible.
   */
  accentText: string
  /** Tinted accent background for pills. */
  accentSoft: string
  /** Text colour to use ON accentSoft. */
  accentInk: string
  /** Text colour to use ON the accent fill. */
  onAccent: string
  okSoft: string
  okInk: string
  /**
   * Shadow under the card.
   *
   * Static, not themed: its reach must stay within PANEL_PAD or macOS clips the
   * blur at the transparent window's edge — see panelShadow.test.ts.
   */
  shadow: string
  radius: number
  rowRadius: number
}

/**
 * The live skin. Kiro Crew's variable names first, this app's older aliases second,
 * `kiro-dark` literals last.
 */
export const THEME_SKIN: PanelSkin = {
  card: 'var(--card, var(--bg-elevated, #211d25))',
  ink: 'var(--card-fg, var(--text, #dcdadf))',
  muted: 'var(--muted, #938f9b)',
  faint: 'var(--muted, #938f9b)',
  row: 'var(--bg-hover, var(--bg-input, #28242e))',
  hairline: 'var(--border, #352f3d)',
  accent: 'var(--accent, #8e48ff)',
  accentText: 'var(--cc-accent-text, var(--card-fg, var(--text, #dcdadf)))',
  accentSoft: 'var(--accent-subtle, rgba(178,127,255,0.18))',
  accentInk: 'var(--accent, #8e48ff)',
  onAccent: 'var(--accent-fg, #ffffff)',
  okSoft: 'var(--ok-subtle, rgba(0,133,67,0.18))',
  okInk: 'var(--ok, #008543)',
  shadow: 'var(--cc-panel-shadow)',
  radius: 16,
  rowRadius: 11,
}

/**
 * Kept as a named export because `mode` no longer selects a palette — the linked
 * stylesheet handles light and dark itself. Callers pass their mode and get the
 * same theme-driven skin, so no call site had to learn about the change.
 */
export function skinFor(_mode?: 'dark' | 'light'): PanelSkin {
  return THEME_SKIN
}

/**
 * The active skin, shared by the card and the breathing overlay.
 *
 * A context rather than a prop chain because the overlay is a SIBLING of the card
 * in panelEntry, and the small pieces (SectionLabel, Pill) are module-level
 * components — threading a skin prop through all of them would be noise.
 */
export const SkinContext = createContext<PanelSkin>(THEME_SKIN)
export const useSkin = (): PanelSkin => useContext(SkinContext)


/**
 * Decide whether this theme's accent is legible as text, and publish the answer as
 * `--cc-accent-text` for the skin to reference.
 *
 * Runs against the COMPUTED values, so it sees whatever the dashboard stylesheet
 * actually resolved to — including themes this app has never seen.
 */
export function resolveAccentText(root: HTMLElement = document.documentElement): void {
  const cs = getComputedStyle(root)
  const read = (name: string) => cs.getPropertyValue(name).trim()

  const accent = read('--accent')
  const card = read('--card') || read('--bg-elevated')
  const ink = read('--card-fg') || read('--text')
  if (!accent || !card || !ink) return // stay on the declared fallback

  root.style.setProperty('--cc-accent-text', pickReadable(accent, card, ink))
}
