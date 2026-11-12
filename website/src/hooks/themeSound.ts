/** Shared contract between app event sources (chat/notification) and the theme
 *  audio layer (ThemeExperienceLayer). App code calls `emitThemeSound(trigger)`
 *  when a real event occurs (e.g. an agent reply arrives); the layer routes the
 *  trigger to the active theme's `audio/manifest.json` entry — gated on consent,
 *  mute, and reduced-motion INSIDE the layer, so callers need no theme state.
 *  Decoupled by a window CustomEvent so neither side imports the other. */
export const MC_THEME_SOUND_EVENT = 'mc-theme-sound' as const

export interface ThemeSoundDetail {
  /** A theme.json audio trigger name, e.g. 'message-received' / 'notification'. */
  trigger: string
}

/** Fire a theme-audio trigger. No-op-safe: if no L2 theme is active, the theme
 *  has no such trigger, or audio is muted/consent-absent, the listener ignores
 *  it. Never throws into the caller's event path. */
export function emitThemeSound(trigger: string): void {
  try {
    const detail: ThemeSoundDetail = { trigger }
    window.dispatchEvent(new CustomEvent(MC_THEME_SOUND_EVENT, { detail }))
  } catch {
    /* no window (SSR) — non-fatal */
  }
}

/** The audio triggers the active theme can play right now: published by the
 *  theme layer only while its sounds are on and audible (consented, not muted,
 *  no reduced motion), and withdrawn when that stops or the layer unmounts.
 *  Read at event time by both notification listeners, so whichever runs first
 *  they agree on who plays a notification. */
let activeThemeTriggers: ReadonlySet<string> | null = null
let activeThemeOwner: symbol | null = null

/** Publish the playable trigger names; returns a release that withdraws them
 *  only while this publication is still the current one. */
export function publishThemeSoundTriggers(triggers: Iterable<string>): () => void {
  const owner = Symbol('theme-sound-triggers')
  activeThemeTriggers = new Set(triggers)
  activeThemeOwner = owner
  return () => {
    if (activeThemeOwner !== owner) return
    activeThemeTriggers = null
    activeThemeOwner = null
  }
}

/** The trigger names published right now; empty when no theme sound can play. */
export function activeThemeSoundTriggers(): ReadonlySet<string> {
  return activeThemeTriggers ?? new Set()
}
