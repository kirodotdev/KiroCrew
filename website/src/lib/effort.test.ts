import { describe, it, expect } from 'vitest'

import { ApiError } from '../api/apiError'
import { effortInForce, effortOffOnAuto, selectionCapabilitiesFailed } from './effort'

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

// #42115: a level the serving model does not offer stays on the slot, and the
// chip names what runs instead.
describe('effortInForce', () => {
  it('names what runs when the session reports the shown own pick unapplied', () => {
    expect(effortInForce('max', { effort_unapplied: 'max', effort_running: 'high' }, true))
      .toEqual({ level: 'high', notOffered: 'max' })
  })

  it('takes the session report for an inherited level', () => {
    expect(effortInForce('medium', { effort_unapplied: 'max', effort_running: 'high', effort_inherited: true }, false))
      .toEqual({ level: 'high', notOffered: 'max' })
  })

  it('ignores a report checked for a pick when the slot has none', () => {
    // A pick cleared elsewhere leaves the answer for the old pick cached.
    expect(effortInForce('medium', { effort_unapplied: 'max', effort_running: 'high' }, false))
      .toEqual({ level: 'medium', notOffered: '' })
  })

  it('names no level when the model runs its own', () => {
    expect(effortInForce('max', { effort_unapplied: 'max', effort_running: '' }, true))
      .toEqual({ level: '', notOffered: 'max' })
    expect(effortInForce('max', { effort_unapplied: 'max' }, true))
      .toEqual({ level: '', notOffered: 'max' })
  })

  it('leaves the shown level alone otherwise', () => {
    expect(effortInForce('high', undefined, true)).toEqual({ level: 'high', notOffered: '' })
    expect(effortInForce('high', {}, true)).toEqual({ level: 'high', notOffered: '' })
    // A newer pick than the one the session reported keeps its own name.
    expect(effortInForce('low', { effort_unapplied: 'max', effort_running: 'high' }, true))
      .toEqual({ level: 'low', notOffered: '' })
  })
})
