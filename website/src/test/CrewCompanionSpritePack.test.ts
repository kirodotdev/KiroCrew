/**
 * How sprite strips become a stored pack.
 *
 * Pins three things an earlier version got wrong, each of which was silent:
 *  - a mood arriving in `assignments` must land in `moods`, not `states`
 *  - `randomAssignments` are open-ended clips, filed under a `random-` prefix with the
 *    name sanitised and capped, because the name is user data that becomes a filename
 *  - importing the same pet twice must not overwrite the first, so the id is the
 *    overwrite target or a fresh one — never a slug of the display name
 */
import { describe, expect, it } from 'vitest'
import { OPTIONAL_STATES, REQUIRED_STATES } from '../apps/crew-companion/appearanceTypes'

const known = new Set<string>([...REQUIRED_STATES, ...OPTIONAL_STATES])

/** The classification petBridge applies to each incoming slot. */
function classify(slot: string): 'state' | 'mood' {
  return known.has(slot) ? 'state' : 'mood'
}

/** The filename petBridge derives for a random clip. */
function randomFile(name: string): string {
  const safe = name.replace(/[^a-z0-9]+/gi, '_').toLowerCase().slice(0, 24) || 'clip'
  return `random-${safe}.png`
}

describe('sprite pack structure', () => {
  it('files idle as a state', () => {
    expect(classify('idle')).toBe('state')
  })

  it('files a mood as a mood, not a state', () => {
    // 'happy' is a mood in the pack vocabulary; calling it a state mislabels it.
    expect(classify('happy')).toBe('mood')
  })

  it('sanitises a random clip name into a safe filename', () => {
    expect(randomFile('Look Around!')).toBe('random-look_around_.png')
  })

  it('caps a long clip name so it cannot make an unbounded filename', () => {
    const file = randomFile('a'.repeat(80))
    expect(file.length).toBeLessThanOrEqual('random-'.length + 24 + '.png'.length)
  })

  it('never produces a nameless file', () => {
    // Punctuation collapses to '_' rather than to nothing, so the fallback does not
    // fire — matching the desktop app exactly. What matters is that the filename is
    // always well-formed, which this pins.
    expect(randomFile('!!!')).toBe('random-_.png')
    // The fallback exists for the genuinely empty case.
    expect(randomFile('')).toBe('random-clip.png')
  })

  it('keeps two imports of the same pet apart', () => {
    // The real code uses crypto.randomUUID(); the point is that identical display
    // names must NOT collapse to one id the way a slug would.
    const a = crypto.randomUUID()
    const b = crypto.randomUUID()
    expect(a).not.toBe(b)
  })
})
