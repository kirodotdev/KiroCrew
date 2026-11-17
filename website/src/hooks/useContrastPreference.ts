import { useCallback, useEffect, useState } from 'react'
import {
  CONTRAST_CHANGED_EVENT,
  PREFERS_MORE_CONTRAST_QUERY,
  contrastFromStorageEvent,
  contrastWanted,
  osPrefersMoreContrast,
  parseContrastPreference,
  persistContrastPreference,
  readContrastPreference,
  type ContrastPreference,
} from '../utils/contrastPreference'

/**
 * State for the "Contrast" display setting (`utils/contrastPreference.ts`).
 * `active` is the answer the shell paints: whether the High Contrast palette is
 * wanted for the stored choice and the OS's current `prefers-contrast` state.
 * Every mounted instance follows `CONTRAST_CHANGED_EVENT` (the event carries the
 * value, so blocked storage cannot undo a flip), the `storage` event for a change
 * made in another tab, and the OS media query, so the
 * Settings row and `useTheme` agree without sharing a provider.
 */
export function useContrastPreference(): {
  pref: ContrastPreference
  active: boolean
  setPref: (pref: ContrastPreference) => void
} {
  const [pref, setState] = useState<ContrastPreference>(() => readContrastPreference())
  const [osMore, setOsMore] = useState<boolean>(() => osPrefersMoreContrast())
  useEffect(() => {
    const onChanged = (event: Event) =>
      setState(parseContrastPreference((event as CustomEvent<unknown>).detail))
    // Another same-origin tab changed the shared store; this tab only reads it.
    const onStorage = (event: StorageEvent) => {
      const next = contrastFromStorageEvent(event)
      if (next !== null) setState(next)
    }
    window.addEventListener(CONTRAST_CHANGED_EVENT, onChanged)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(CONTRAST_CHANGED_EVENT, onChanged)
      window.removeEventListener('storage', onStorage)
    }
  }, [])
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const mql = window.matchMedia(PREFERS_MORE_CONTRAST_QUERY)
    const onOsChange = () => setOsMore(mql.matches)
    onOsChange()
    mql.addEventListener('change', onOsChange)
    return () => mql.removeEventListener('change', onOsChange)
  }, [])
  const setPref = useCallback((next: ContrastPreference) => {
    persistContrastPreference(next)
    setState(next)
    window.dispatchEvent(new CustomEvent(CONTRAST_CHANGED_EVENT, { detail: next }))
  }, [])
  return { pref, active: contrastWanted(pref, osMore), setPref }
}
