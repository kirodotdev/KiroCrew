import { describe, expect, it } from 'vitest'
import { MOOD_KEYS, moodLookFor, moodTheme } from './moods'

describe('mood -> theme', () => {
  it('gives every chip its own look, and an unknown chip the neutral one', () => {
    const seen = new Set(MOOD_KEYS.map(k => moodTheme(k).hues.join(',')))
    expect(seen.size).toBe(MOOD_KEYS.length)
    expect(moodTheme('angry-ish')).toEqual(moodTheme('neutral'))
    expect(moodTheme(undefined)).toEqual(moodTheme('neutral'))
  })

  it('drifts a calm mood slower than a fired-up one', () => {
    expect(moodTheme('calm').driftSeconds).toBeGreaterThan(moodTheme('fired').driftSeconds)
  })

  it('places free text and agent tags on a chip, or neutral', () => {
    expect(moodLookFor(['今天好想念外婆'])).toBe('missing')
    expect(moodLookFor(['Feeling PUMPED'])).toBe('fired')
    expect(moodLookFor(['温柔', '平静'])).toBe('calm')
    expect(moodLookFor([''])).toBe('neutral')
    expect(moodLookFor(['something else'])).toBe('neutral')
  })
})
