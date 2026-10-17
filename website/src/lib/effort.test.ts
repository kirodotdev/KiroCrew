import { describe, it, expect } from 'vitest'

import { ApiError } from '../api/apiError'
import { selectionCapabilitiesFailed } from './effort'

describe('selectionCapabilitiesFailed', () => {
  it('is false when the read has not errored', () => {
    expect(selectionCapabilitiesFailed({ isError: false })).toBe(false)
  })

  it('treats a 404 (slot not registered yet) as unknown, not failed', () => {
    expect(selectionCapabilitiesFailed({ isError: true, error: new ApiError(404, 'not found') })).toBe(false)
  })

  it('counts real faults as failures', () => {
    expect(selectionCapabilitiesFailed({ isError: true, error: new ApiError(503, 'peer unavailable') })).toBe(true)
    expect(selectionCapabilitiesFailed({ isError: true, error: new ApiError(403, 'forbidden') })).toBe(true)
    expect(selectionCapabilitiesFailed({ isError: true, error: new TypeError('Failed to fetch') })).toBe(true)
    expect(selectionCapabilitiesFailed({ isError: true })).toBe(true)
  })
})
