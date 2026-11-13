import { describe, it, expect } from 'vitest'

import { ApiError } from '../api/apiError'
import { effortOffOnAuto, selectionCapabilitiesFailed } from './effort'

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

// #11546: on `auto` the kiro backend refuses an effort level, so the picker has
// no slider and must say why rather than leave the configured default looking live.
describe('effortOffOnAuto', () => {
  it('is true on auto when the provider takes effort but no control is offered', () => {
    expect(effortOffOnAuto(true, false, false, 'auto')).toBe(true)
    expect(effortOffOnAuto(true, false, false, ' Auto ')).toBe(true)
  })

  it('is false when an effort control is offered', () => {
    expect(effortOffOnAuto(true, true, false, 'auto')).toBe(false)
  })

  it('is false on a specific model that takes no effort', () => {
    expect(effortOffOnAuto(true, false, false, 'deepseek-v4')).toBe(false)
    expect(effortOffOnAuto(true, false, false, '')).toBe(false)
  })

  it('is false when the provider has no effort at all', () => {
    expect(effortOffOnAuto(false, false, false, 'auto')).toBe(false)
  })

  it('leaves a failed capability read to its own notice', () => {
    expect(effortOffOnAuto(true, false, true, 'auto')).toBe(false)
  })
})
