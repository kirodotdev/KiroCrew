import { describe, it, expect } from 'vitest'
import { displayModel, normalizeModelKey, pinIsWithheld } from '../lib/model'

/** The picker's list is narrowed to what the live session says the account can
 *  run, while the slot keeps whatever model was pinned before. After a plan
 *  downgrade those disagree, and the backend withholds the pin and runs the
 *  session on its default — so the composer must name the model that will
 *  actually run, not the dead pin. */
describe('displayModel', () => {
  const list = [
    { name: 'auto' },
    { name: 'claude-sonnet-5' },
    { name: 'claude-opus-4.8' },
  ]

  it('shows a pin that is still on the list', () => {
    expect(displayModel('claude-sonnet-5', list)).toBe('claude-sonnet-5')
  })

  it('falls back to auto for a pin the list no longer offers', () => {
    expect(displayModel('claude-opus-5', list)).toBe('auto')
  })

  it('returns the list spelling so the row actually highlights', () => {
    // Matching is normalized across dotted/dashed/case, but ModelDropdownList
    // highlights on exact `activeModel === m.name`. Returning the caller's
    // spelling would show the model in the chip while checking no row.
    expect(displayModel('claude-opus-4-8', list)).toBe('claude-opus-4.8')
    expect(displayModel('CLAUDE-OPUS-4.8', list)).toBe('claude-opus-4.8')
    expect(displayModel('claude-opus-4.8', list)).toBe('claude-opus-4.8')
  })

  it('keeps the pin when the list is marked degraded', () => {
    // The blocking case: /api/models is failing, React Query keeps serving the
    // last successful list, and that list predates the account regaining access.
    // It looks healthy by LENGTH while being arbitrarily stale, so only the
    // explicit degraded signal can be trusted. Relabelling here would let the
    // "set default for agent" row persist 'auto' over a pin that is valid.
    expect(displayModel('claude-opus-5', list, true)).toBe('claude-opus-5')
  })

  it('keeps the pin when the list is empty', () => {
    expect(displayModel('claude-opus-5', [])).toBe('claude-opus-5')
  })

  it('does not treat list length as the degraded signal', () => {
    // A live backend advertising only auto genuinely offers only auto, so a pin
    // absent from it IS withheld. Length is not evidence either way — that is
    // what the degraded flag is for.
    expect(displayModel('claude-opus-5', [{ name: 'auto' }])).toBe('auto')
    expect(displayModel('claude-opus-5', [{ name: 'auto' }], true)).toBe('claude-opus-5')
  })

  it('renders an unset or auto slot as auto', () => {
    expect(displayModel('', list)).toBe('auto')
    expect(displayModel('auto', list)).toBe('auto')
    expect(displayModel('default', list)).toBe('auto')
  })
})

describe('pinIsWithheld', () => {
  it('is true when a real pin displays as auto', () => {
    expect(pinIsWithheld('claude-opus-5', 'auto')).toBe(true)
  })

  it('is false for a mere spelling difference', () => {
    // displayModel returns the list's spelling, so pin and shown can differ as
    // strings while naming the same model. Treating that as withheld would
    // disable the pin row for a perfectly usable model.
    expect(pinIsWithheld('claude-opus-4.8', 'claude-opus-4-8')).toBe(false)
    expect(pinIsWithheld('CLAUDE-OPUS-4.8', 'claude-opus-4.8')).toBe(false)
  })

  it('is false when nothing is pinned', () => {
    expect(pinIsWithheld('', 'auto')).toBe(false)
    expect(pinIsWithheld('auto', 'auto')).toBe(false)
  })

  it('is false when the pin is displayed as itself', () => {
    expect(pinIsWithheld('claude-sonnet-5', 'claude-sonnet-5')).toBe(false)
  })
})

describe('normalizeModelKey', () => {
  it('folds case, dots and the auto/default synonyms', () => {
    expect(normalizeModelKey('Claude-Opus-4.8')).toBe('claude-opus-4-8')
    expect(normalizeModelKey(' auto ')).toBe('auto')
    expect(normalizeModelKey('default')).toBe('auto')
    expect(normalizeModelKey('')).toBe('')
  })
})
