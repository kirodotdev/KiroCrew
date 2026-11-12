import { describe, expect, it } from 'vitest'

import { MODEL_UNAVAILABLE_KIND, isModelUnavailable } from './modelUnavailable'

describe('isModelUnavailable', () => {
  it('reads both carriers on an error row', () => {
    expect(isModelUnavailable({ role: 'error', kind: MODEL_UNAVAILABLE_KIND })).toBe(true)
    expect(isModelUnavailable({ role: 'error', meta: { kind: MODEL_UNAVAILABLE_KIND } })).toBe(true)
  })

  it('refuses other kinds and other roles', () => {
    expect(isModelUnavailable({ role: 'error', meta: { kind: 'model_unentitled' } })).toBe(false)
    expect(isModelUnavailable({ role: 'error' })).toBe(false)
    expect(isModelUnavailable({ role: 'assistant', meta: { kind: MODEL_UNAVAILABLE_KIND } })).toBe(false)
  })

  it('spells the backend kind', () => {
    // chat_utils.MODEL_UNAVAILABLE_KIND; test_model_unavailable_settles_turn.py pins the Python side.
    expect(MODEL_UNAVAILABLE_KIND).toBe('model_unavailable')
  })
})
