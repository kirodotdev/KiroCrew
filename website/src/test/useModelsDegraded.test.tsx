import { describe, expect, it, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import {
  markModelsDegraded,
  modelsDegraded,
  useModelsDegraded,
} from '../providers/modelListHealth'

describe('useModelsDegraded', () => {
  beforeEach(() => {
    // The store is module-level; reset both providers used below.
    markModelsDegraded('acp', false)
    markModelsDegraded('other', false)
  })

  it('re-renders when the flag flips with no change to the model list', () => {
    // The scenario the plain getter cannot serve: a failed /api/models resolves
    // SUCCESSFULLY with the last-good cached list, so React Query hands back a
    // structurally identical result and notifies nobody. Only a subscription
    // sees the flag move, and the composer's displayed model depends on it.
    const { result } = renderHook(() => useModelsDegraded('acp'))
    expect(result.current).toBe(false)

    act(() => {
      markModelsDegraded('acp', true)
    })
    expect(result.current).toBe(true)

    act(() => {
      markModelsDegraded('acp', false)
    })
    expect(result.current).toBe(false)
  })

  it('is provider-scoped', () => {
    const { result } = renderHook(() => useModelsDegraded('acp'))
    act(() => {
      markModelsDegraded('other', true)
    })
    expect(result.current).toBe(false)
  })

  it('reports false for a provider that never fetched', () => {
    const { result } = renderHook(() => useModelsDegraded('never-seen'))
    expect(result.current).toBe(false)
  })

  it('agrees with the non-reactive getter', () => {
    // The refetch-cadence path still reads the getter, so the two must not
    // diverge.
    const { result } = renderHook(() => useModelsDegraded('acp'))
    act(() => {
      markModelsDegraded('acp', true)
    })
    expect(result.current).toBe(modelsDegraded('acp'))
  })
})
