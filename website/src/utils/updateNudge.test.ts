import { describe, it, expect } from 'vitest'

import { shouldNudge, snoozeRecord, skipRecord, SNOOZE_SECS } from './updateNudge'

/**
 * The per-version interruption policy. Every rule here guards against a
 * SILENT failure mode: a popup that nags a user who already answered, or one
 * that never returns for the release after the one they skipped.
 */
describe('updateNudge policy', () => {
  const now = 1_756_000_000

  it('never nudges without a version', () => {
    expect(shouldNudge('', undefined, now)).toBe(false)
    expect(shouldNudge(undefined, { version: '1.0.0' }, now)).toBe(false)
  })

  it('nudges when no record exists', () => {
    expect(shouldNudge('1.0.0', undefined, now)).toBe(true)
    expect(shouldNudge('1.0.0', {}, now)).toBe(true)
  })

  it('a skip silences only the version it names', () => {
    const rec = skipRecord('1.0.0')
    expect(shouldNudge('1.0.0', rec, now)).toBe(false)
    // The NEXT release must get its one proactive prompt again.
    expect(shouldNudge('1.1.0', rec, now)).toBe(true)
  })

  it('a snooze silences until it lapses, then nudges again', () => {
    const rec = snoozeRecord('1.0.0', now)
    expect(rec.snoozed_until).toBe(now + SNOOZE_SECS)
    expect(shouldNudge('1.0.0', rec, now)).toBe(false)
    expect(shouldNudge('1.0.0', rec, now + SNOOZE_SECS - 1)).toBe(false)
    expect(shouldNudge('1.0.0', rec, now + SNOOZE_SECS)).toBe(true)
  })

  it('a snooze for one version never suppresses another', () => {
    const rec = snoozeRecord('1.0.0', now)
    expect(shouldNudge('1.1.0', rec, now)).toBe(true)
  })

  it('skipRecord marks skipped without a snooze horizon', () => {
    expect(skipRecord('2.0.0')).toEqual({ version: '2.0.0', snoozed_until: 0, skipped: true })
  })
})
