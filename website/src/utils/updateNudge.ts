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
}

/** How long "Remind me tomorrow" stays quiet, in seconds. */
export const SNOOZE_SECS = 24 * 60 * 60

/**
 * Should the popup interrupt for `version` right now?
 *
 * A record for a DIFFERENT version never suppresses: a snooze or skip is a
 * verdict on one specific release, and the next release must get its one
 * proactive prompt regardless.
 */
export function shouldNudge(
  version: string | undefined,
  record: UpdateNudgeRecord | undefined,
  nowSecs: number,
): boolean {
  if (!version) return false
  if (!record || record.version !== version) return true
  if (record.skipped) return false
  return (record.snoozed_until ?? 0) <= nowSecs
}

/** The record "Remind me tomorrow" (and a plain dismissal) writes. */
export function snoozeRecord(version: string, nowSecs: number): Required<UpdateNudgeRecord> {
  return { version, snoozed_until: nowSecs + SNOOZE_SECS, skipped: false }
}

/** The record "Skip this version" writes. */
export function skipRecord(version: string): Required<UpdateNudgeRecord> {
  return { version, snoozed_until: 0, skipped: true }
}
