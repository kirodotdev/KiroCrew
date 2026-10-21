import { describe, expect, it } from 'vitest'
import { deckStartedSince } from '../apps/pptx-maker/studioLayout'

describe('deckStartedSince', () => {
  it('returns the newest deck that was not present when the chat started', () => {
    const known = new Set(['20260929-1510-old'])
    expect(deckStartedSince(['20260930-0900-new', '20260929-1510-old'], known)).toBe(
      '20260930-0900-new',
    )
  })

  it('returns null while the chat has not created a deck yet', () => {
    const known = new Set(['20260929-1510-old'])
    expect(deckStartedSince(['20260929-1510-old'], known)).toBeNull()
  })

  it('treats every deck as new when there were none before', () => {
    expect(deckStartedSince(['20260930-0900-new'], new Set())).toBe('20260930-0900-new')
  })
})
