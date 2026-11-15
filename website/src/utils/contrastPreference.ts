/**
 * The "Contrast" display setting (Settings -> Display): whether the shell paints
 * the built-in High Contrast palette.
 *
 * Three choices, stored as a browser-local preference like the theme mode:
 * - `normal` (the default, and what an absent or unreadable store means): the
 *   user's chosen theme, exactly as before this setting existed.
 * - `auto`: follow the operating system. The High Contrast palette is painted
 *   while the OS asks for more contrast (`prefers-contrast: more`) and the
 *   chosen theme otherwise.
 * - `more`: always paint the High Contrast palette.
 *
 * The chosen theme is never overwritten: the palette is swapped only at the
 * moment the root `data-theme` attribute is written (`useTheme`'s `applyTheme`),
 * so going back to `normal`, or the OS dropping its request, restores the pick.
 */
export type ContrastPreference = 'normal' | 'auto' | 'more'

export const CONTRAST_STORAGE_KEY = 'mc-contrast'
export const CONTRAST_CHANGED_EVENT = 'mc-contrast-changed'
export const PREFERS_MORE_CONTRAST_QUERY = '(prefers-contrast: more)'

/** The built-in theme slug painted while contrast is wanted (`THEMES` in useTheme). */
export const HIGH_CONTRAST_THEME = 'highcontrast'

export function parseContrastPreference(raw: unknown): ContrastPreference {
  return raw === 'auto' || raw === 'more' ? raw : 'normal'
}

/**
 * The choice as set in this page session when the store refused it (quota,
 * private mode). Held so a reader mounted later, such as the Display panel
 * after another Settings tab, still agrees with the shell. Any write the store
 * accepts clears it.
 */
let unstoredChoice: ContrastPreference | null = null

export function readContrastPreference(): ContrastPreference {
  if (unstoredChoice !== null) return unstoredChoice
  try {
    return parseContrastPreference(localStorage.getItem(CONTRAST_STORAGE_KEY))
  } catch {
    return 'normal'
  }
}

export function persistContrastPreference(pref: ContrastPreference): void {
  try {
    if (pref === 'normal') localStorage.removeItem(CONTRAST_STORAGE_KEY)
    else localStorage.setItem(CONTRAST_STORAGE_KEY, pref)
    unstoredChoice = null
  } catch {
    // Storage may be unavailable (private mode, quota); the choice still holds for this session.
    unstoredChoice = pref
  }
}

/**
 * The choice another same-origin tab just stored, read from its `storage` event;
 * `null` when the event is about something else. Removing the key, or clearing
 * the whole store (`event.key` is `null`), reads as `normal`. The store now holds
 * what that tab wrote, so a choice this tab was keeping only in memory is
 * dropped. Nothing is written back: the other tab already did.
 */
export function contrastFromStorageEvent(event: StorageEvent): ContrastPreference | null {
  if (event.key !== null && event.key !== CONTRAST_STORAGE_KEY) return null
  try {
    if (event.storageArea && event.storageArea !== localStorage) return null
  } catch {
    return null
  }
  unstoredChoice = null
  return event.key === null ? 'normal' : parseContrastPreference(event.newValue)
}

/** Whether the operating system is asking for more contrast right now. */
export function osPrefersMoreContrast(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  try {
    return window.matchMedia(PREFERS_MORE_CONTRAST_QUERY).matches
  } catch {
    return false
  }
}

/** Whether the High Contrast palette should be painted for this choice and OS state. */
export function contrastWanted(pref: ContrastPreference, osMore: boolean): boolean {
  return pref === 'more' || (pref === 'auto' && osMore)
}

/** `contrastWanted` for the stored choice and the live OS state. */
export function contrastActive(): boolean {
  return contrastWanted(readContrastPreference(), osPrefersMoreContrast())
}
