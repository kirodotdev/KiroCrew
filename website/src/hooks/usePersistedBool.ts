import { useState, useEffect } from 'react'
import { safeSetItem } from '../utils/safeStorage'

/**
 * Boolean state persisted to localStorage — used for view preferences that
 * should survive tab switches and reloads (word wrap, line numbers, diff
 * split/unified, …). Reads once on mount; writes (via safeSetItem, quota-
 * defensive) whenever the value changes. Instances sharing a key don't
 * live-sync — each picks up the persisted value on its next mount, which is
 * fine for the toolbar-toggle use case.
 */
export function usePersistedBool(key: string, defaultValue: boolean) {
  const [value, setValue] = useState<boolean>(() => {
    try {
      const v = localStorage.getItem(key)
      return v === null ? defaultValue : v === '1'
    } catch {
      return defaultValue
    }
  })
  useEffect(() => { safeSetItem(key, value ? '1' : '0') }, [key, value])
  return [value, setValue] as const
}
