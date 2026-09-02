/**
 * Command-palette provider registry — registration, replacement, lookup, reset.
 *
 * The registry is module-global mutable state, so the invariants worth pinning
 * are: registration order is preserved, re-registering an id REPLACES rather
 * than appends (idempotent across repeated side-effect imports / hot reload),
 * and the test reset actually empties it.
 */
import { describe, expect, it, beforeEach } from 'vitest'
import {
  registerProvider,
  getProviders,
  getProvider,
  _resetProvidersForTest,
} from './index'
import type { ResourceProvider } from '../types'

function provider(id: string, label = `zzq-${id}`): ResourceProvider {
  return { id, label, icon: null, search: () => [] }
}

beforeEach(() => {
  _resetProvidersForTest()
})

describe('registerProvider', () => {
  it('keeps registration order', () => {
    registerProvider(provider('zzq-a'))
    registerProvider(provider('zzq-b'))
    expect(getProviders().map((p) => p.id)).toEqual(['zzq-a', 'zzq-b'])
  })

  it('replaces an existing id in place instead of appending a duplicate', () => {
    registerProvider(provider('zzq-a'))
    registerProvider(provider('zzq-b'))
    registerProvider(provider('zzq-a', 'zzq-replaced'))
    expect(getProviders().map((p) => p.id)).toEqual(['zzq-a', 'zzq-b'])
    expect(getProviders()[0].label).toBe('zzq-replaced')
  })
})

describe('getProvider', () => {
  it('finds a registered provider by id', () => {
    const p = provider('zzq-sessions')
    registerProvider(p)
    expect(getProvider('zzq-sessions')).toBe(p)
  })

  it('returns undefined for an unknown id', () => {
    registerProvider(provider('zzq-sessions'))
    expect(getProvider('zzq-missing')).toBeUndefined()
  })
})

describe('_resetProvidersForTest', () => {
  it('empties the registry', () => {
    registerProvider(provider('zzq-a'))
    _resetProvidersForTest()
    expect(getProviders()).toHaveLength(0)
    expect(getProvider('zzq-a')).toBeUndefined()
  })
})
