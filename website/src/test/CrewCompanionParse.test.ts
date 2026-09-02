/**
 * Reminder parsing: schedule spans are UTF-16 offsets.
 *
 * `cleanText` blanks the matched schedule ranges by index. Those indices come
 * from regex `.index`, which counts UTF-16 CODE UNITS, so the character array it
 * indexes into must be built the same way. Building it with `[...original]`
 * (which iterates CODE POINTS) makes every index after an astral character off
 * by one, and the wrong characters get blanked -- the saved reminder is silently
 * corrupted, not merely mis-scheduled.
 *
 * An emoji in a reminder is entirely ordinary ("💧 drink water"), so this is a
 * real path and not a synthetic edge case.
 */
import { describe, it, expect } from 'vitest'
import { parseReminder } from '../apps/crew-companion/reminderParse'

const NOW = new Date('2026-08-01T10:00:00Z')

describe('parseReminder with astral characters', () => {
  it('keeps the whole text when the reminder starts with an emoji', () => {
    const r = parseReminder('💧 drink water in 20 minutes', NOW, 'Reminder')
    expect(r).not.toBeNull()
    // The bug truncated this to "💧 drink water i" -- one lost character per
    // surrogate pair ahead of the span.
    expect(r!.text).toBe('💧 drink water')
  })

  it('is unaffected for the same phrase without the emoji', () => {
    const plain = parseReminder('drink water in 20 minutes', NOW, 'Reminder')
    expect(plain!.text).toBe('drink water')
  })

  it('survives several astral characters before the schedule', () => {
    const r = parseReminder('🧊💧🚰 refill the bottle every 2 hours', NOW, 'Reminder')
    expect(r).not.toBeNull()
    expect(r!.text).toBe('🧊💧🚰 refill the bottle')
    expect(r!.recurrence?.everyMinutes).toBe(120)
  })

  it('does not split a surrogate pair', () => {
    const r = parseReminder('stretch 🙆 in 1 hour', NOW, 'Reminder')
    // Not a surrogate-range scan: an INTACT pair is itself two surrogates, so
    // that would always match. A split pair shows up as a replacement char, and
    // the emoji must survive as one whole code point.
    expect(r!.text).not.toContain('\uFFFD')
    expect(r!.text).toContain('🙆')
    expect([...r!.text].filter((c) => c === '🙆')).toHaveLength(1)
  })
})
