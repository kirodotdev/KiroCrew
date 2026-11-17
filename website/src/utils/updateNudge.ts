/**
 * Per-version policy for the proactive "update found" popup.
 *
 * One decision function shared by every update source (the Electron desktop
 * updater and the gateway's own feed check), so the popup cannot develop
 * per-platform nudge behaviour: the sources differ only in where the update
 * event comes from and which action the primary button can offer — WHETHER to
 * interrupt is decided here, once.
 *
 * The record persists in gateway config (`dashboard.update_nudge.*`) rather
 * than browser localStorage, so a decision like "skip 0.5.0" holds across
 * browsers, windows, and the desktop app's embedded dashboard — they all talk
 * to the same gateway.
 */

export type UpdateNudgeRecord = {
  /** The version the snooze/skip below applies to. */
  version?: string
  /** Epoch seconds. The popup stays quiet for `version` until this passes. */
  snoozed_until?: number
  /** True = never proactively pop for `version` again. */
  skipped?: boolean
  /**
   * Every version the user skipped, newest last. The gateway owns it: a
   * verdict below never sends it, and the config PATCH keeps the saved skips
   * and adds this one, so snoozing one release (even from a window holding a
   * stale record) cannot forget the skip of another.
   */
  skipped_versions?: string[]
}

/** What one popup verdict writes; the gateway fills in `skipped_versions`. */
export type UpdateNudgeVerdict = Required<Omit<UpdateNudgeRecord, 'skipped_versions'>>

/** How long "Remind me tomorrow" stays quiet, in seconds. */
export const SNOOZE_SECS = 24 * 60 * 60

/** The skips `record` holds, including a skip stored before the list existed. */
function skipsOf(record: UpdateNudgeRecord | undefined): string[] {
  const list = Array.isArray(record?.skipped_versions)
    ? record.skipped_versions.filter((v): v is string => typeof v === 'string' && v !== '')
    : []
  if (record?.skipped && record.version && !list.includes(record.version)) list.push(record.version)
  return list
}

/**
 * Should the popup interrupt for `version` right now?
 *
 * A snooze for a DIFFERENT version never suppresses: the next release must get
 * its one proactive prompt regardless. A skip holds for its own version only,
 * whatever later verdict the record carries.
 */
export function shouldNudge(
  version: string | undefined,
  record: UpdateNudgeRecord | undefined,
  nowSecs: number,
): boolean {
  if (!version) return false
  if (skipsOf(record).includes(version)) return false
  if (!record || record.version !== version) return true
  return (record.snoozed_until ?? 0) <= nowSecs
}

/** The record "Remind me tomorrow" (and a plain dismissal) writes. */
export function snoozeRecord(version: string, nowSecs: number): UpdateNudgeVerdict {
  return { version, snoozed_until: nowSecs + SNOOZE_SECS, skipped: false }
}

/** The record "Skip this version" writes. */
export function skipRecord(version: string): UpdateNudgeVerdict {
  return { version, snoozed_until: 0, skipped: true }
}
