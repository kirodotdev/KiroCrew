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
