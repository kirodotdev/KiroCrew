import { describe, expect, it } from 'vitest'
import { shortenTurnPreview } from './TurnNavigationMinimap'

// At a 64-character cap the budget is 61 units. With no space to break on, the
// cut falls at unit 61.
describe('shortenTurnPreview with astral characters', () => {
  it('drops an emoji the cut would split instead of keeping its high surrogate', () => {
    // 60 ASCII units, then an emoji occupying UTF-16 units 60-61.
    const value = `${'a'.repeat(60)}🙂🙂🙂🙂🙂🙂`
    expect(shortenTurnPreview(value, 64)).toBe(`${'a'.repeat(60)}...`)
  })

  it('keeps an emoji that ends exactly at the cut', () => {
    const value = `${'a'.repeat(59)}🙂${'b'.repeat(10)}`
    expect(shortenTurnPreview(value, 64)).toBe(`${'a'.repeat(59)}🙂...`)
  })

  it('leaves ASCII previews at the budget plus the ellipsis', () => {
    expect(shortenTurnPreview('y'.repeat(80), 64)).toBe(`${'y'.repeat(61)}...`)
  })
})
