/**
 * The sessions whose "interrupted" banner the user closed.
 *
 * Closing the Sessions sidebar's Resume-all banner hides it for the sessions it
 * listed at that moment. Those session keys are kept as a JSON string array under
 * one localStorage key, so the choice survives a reload. A session that becomes
 * interrupted later is not in the array, so the banner returns for it alone.
 * The caller drops a key from the array once its session is no longer
 * interrupted, so a later interruption of the same session is announced again.
 *
 * Deliberately not in `DURABLE_PREF_KEYS`: it is tied to the sessions on this
 * gateway, so a copy restored onto another profile would hide the banner for
 * sessions it has never seen.
 */
import { safeGetItem, safeRemoveItem, safeSetItem } from '../utils/safeStorage'

/** localStorage key holding the JSON string array of dismissed session keys. */
export const RESUME_ALL_DISMISSED_KEY = 'mc-resume-all-dismissed'

/** The dismissed session keys. Absent, malformed or non-array data reads as the
 *  empty set (nothing dismissed), and non-string entries are dropped. Never
 *  throws. */
export function readResumeAllDismissed(): Set<string> {
  const raw = safeGetItem(RESUME_ALL_DISMISSED_KEY)
  if (raw === null) return new Set()
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((key): key is string => typeof key === 'string'))
  } catch {
    return new Set()
  }
}

/** Persist *keys* as the dismissed set. An empty set removes the entry. */
export function writeResumeAllDismissed(keys: ReadonlySet<string>): boolean {
  if (keys.size === 0) {
    safeRemoveItem(RESUME_ALL_DISMISSED_KEY)
    return true
  }
  return safeSetItem(RESUME_ALL_DISMISSED_KEY, JSON.stringify([...keys]))
}
