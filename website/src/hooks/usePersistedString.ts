import { useState, useEffect } from 'react'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'

/**
 * Read a persisted string without subscribing to it.
 *
 * Exported so a caller can seed a SECOND piece of state from the same key on the
 * same first render — e.g. a form field and the "value we last submitted"
 * mirror, which must agree on mount or the first request goes out with the
 * default instead of the remembered value.
 */
export function readPersistedString(key: string, defaultValue: string): string {
  const v = safeGetItem(key)
  return v === null ? defaultValue : v
}

/**
 * String state persisted to localStorage — the sibling of `usePersistedBool`,
 * for text preferences that should survive tab switches, navigation and reloads.
 * Reads once on mount; writes (via quota-defensive `safeSetItem`) on change.
 * Instances sharing a key don't live-sync; each picks the value up on its next
 * mount, which is what a form field needs.
 *
 * Only for non-secret values. Anything credential-shaped does not belong in
 * localStorage.
 */
export function usePersistedString(key: string, defaultValue: string) {
  const [value, setValue] = useState<string>(() => readPersistedString(key, defaultValue))
  useEffect(() => { safeSetItem(key, value) }, [key, value])
  return [value, setValue] as const
}
