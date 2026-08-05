import { describe, it, expect, vi } from 'vitest'
import { createAllAggregator, type AllAggregatorDeps } from './allAggregator'
import type { ResourceProvider, Result } from '../types'

/**
 * Unit tests for the pure {@link createAllAggregator} factory (Search
 * Everywhere). The aggregator fans the query out to injected providers
 * (top-N each, blended by score), and shows injected recents on an empty query.
 */

function result(id: string, providerId: string, title: string, score: number): Result {
  return {
    id,
    providerId,
    title,
    icon: null,
    score,
    indices: [],
    onActivate: vi.fn(),
  }
}

/** A stub provider returning a fixed result set (or throwing). */
function provider(
  id: string,
  results: Result[] | (() => never),
): ResourceProvider {
  return {
    id,
    label: id,
    icon: null,
    search: typeof results === 'function' ? results : () => results,
  }
}

function make(over: Partial<AllAggregatorDeps> & { getProviders: AllAggregatorDeps['getProviders'] }) {
  return createAllAggregator(over)
}

describe('createAllAggregator — identity', () => {
  it('exposes the all provider id, label, and an icon node', () => {
    const p = make({ getProviders: () => [] })
    expect(p.id).toBe('all')
    expect(p.label).toBe('All')
    expect(p.icon).toBeTruthy()
  })
})

describe('createAllAggregator — fan-out + merge', () => {
  it('interleaves results from all providers ordered by score descending', async () => {
    const a = provider('a', [result('a:1', 'a', 'A high', 30), result('a:2', 'a', 'A low', 10)])
    const b = provider('b', [result('b:1', 'b', 'B mid', 20)])
    const agg = make({ getProviders: () => [a, b] })

    const arr = await Promise.resolve(agg.search('q'))
    expect(arr.map((r) => r.id)).toEqual(['a:1', 'b:1', 'a:2'])
  })

  it('caps each provider to perProviderLimit (default 5) after its own ordering', async () => {
    const many = Array.from({ length: 7 }, (_, i) => result(`a:${i}`, 'a', `A${i}`, 70 - i * 10))
    const a = provider('a', many)
    const agg = make({ getProviders: () => [a] })

    const arr = await Promise.resolve(agg.search('q'))
    expect(arr).toHaveLength(5)
    // The five highest-scoring (70,60,50,40,30) survive; the two lowest drop.
    expect(arr.map((r) => r.score)).toEqual([70, 60, 50, 40, 30])
  })

  it('honors an overridden perProviderLimit', async () => {
    const many = Array.from({ length: 4 }, (_, i) => result(`a:${i}`, 'a', `A${i}`, 40 - i * 10))
    const agg = make({ getProviders: () => [provider('a', many)], perProviderLimit: 2 })

    const arr = await Promise.resolve(agg.search('q'))
    expect(arr).toHaveLength(2)
    expect(arr.map((r) => r.score)).toEqual([40, 30])
  })

  it('excludes the aggregator itself from the fan-out', async () => {
    const self = provider('all', [result('all:1', 'all', 'should not appear', 99)])
    const a = provider('a', [result('a:1', 'a', 'A', 10)])
    const agg = make({ getProviders: () => [self, a] })

    const arr = await Promise.resolve(agg.search('q'))
    expect(arr.map((r) => r.id)).toEqual(['a:1'])
  })

  it('survives a provider that throws (contributes nothing, others still appear)', async () => {
    const boom = provider('boom', () => {
      throw new Error('provider exploded')
    })
    const a = provider('a', [result('a:1', 'a', 'A', 10)])
    const agg = make({ getProviders: () => [boom, a] })

    const arr = await Promise.resolve(agg.search('q'))
    expect(arr.map((r) => r.id)).toEqual(['a:1'])
  })
})

describe('createAllAggregator — recents on empty query', () => {
  it('returns recents (never a fan-out) for an empty query', async () => {
    const search = vi.fn(() => [result('a:1', 'a', 'A', 10)])
    const a: ResourceProvider = { id: 'a', label: 'a', icon: null, search }
    const recents = [result('r:1', 'sessions', 'Recent 1', 0), result('r:2', 'sessions', 'Recent 2', 0)]
    const getRecents = vi.fn(() => recents)
    const agg = make({ getProviders: () => [a], getRecents })

    const arr = await Promise.resolve(agg.search('   '))
    expect(arr.map((r) => r.id)).toEqual(['r:1', 'r:2'])
    expect(getRecents).toHaveBeenCalledWith(8) // default recentsLimit
    expect(search).not.toHaveBeenCalled() // no fan-out on empty query
  })

  it('caps recents at recentsLimit', async () => {
    const recents = Array.from({ length: 10 }, (_, i) => result(`r:${i}`, 'sessions', `R${i}`, 0))
    const agg = make({ getProviders: () => [], getRecents: () => recents, recentsLimit: 3 })

    const arr = await Promise.resolve(agg.search(''))
    expect(arr).toHaveLength(3)
  })

  it('returns an empty list for an empty query when no recents source is supplied', async () => {
    const agg = make({ getProviders: () => [provider('a', [result('a:1', 'a', 'A', 10)])] })
    expect(await Promise.resolve(agg.search(''))).toEqual([])
  })
})
