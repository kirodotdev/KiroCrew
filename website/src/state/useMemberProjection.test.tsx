import { describe, it, expect, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'

import { memberProjectionStore } from './memberProjectionStore'
import { useMemberProjection, useMemberProjections, useMemberRosterViews } from './useMemberProjection'

describe('useMemberProjection', () => {
  beforeEach(() => {
    memberProjectionStore.clear()
  })

  it('returns undefined for a null slug and subscribes to nothing', () => {
    const { result } = renderHook(() => useMemberProjection(null, 'roster'))
    expect(result.current).toBeUndefined()
    // Pushing a frame does not throw or wake a null-slug hook.
    act(() => { memberProjectionStore.apply('a', 'roster', { name: 'A' }, 1) })
    expect(result.current).toBeUndefined()
  })

  it('reads the current value on mount', () => {
    memberProjectionStore.apply('a', 'roster', { name: 'A' }, 1)
    const { result } = renderHook(() => useMemberProjection<{ name: string }>('a', 'roster'))
    expect(result.current).toEqual({ name: 'A' })
  })

  it('re-renders when the subscribed key changes', () => {
    const { result } = renderHook(() => useMemberProjection<{ name: string }>('a', 'roster'))
    expect(result.current).toBeUndefined()
    act(() => { memberProjectionStore.apply('a', 'roster', { name: 'A2' }, 2) })
    expect(result.current).toEqual({ name: 'A2' })
  })

  it('does not re-render for a different key on the same slug', () => {
    let renders = 0
    const { result } = renderHook(() => {
      renders += 1
      return useMemberProjection('a', 'roster')
    })
    const afterMount = renders
    act(() => { memberProjectionStore.apply('a', 'wake', { patrol: 'armed' }, 1) })
    expect(renders).toBe(afterMount)
    expect(result.current).toBeUndefined()
  })
})

describe('useMemberProjections', () => {
  beforeEach(() => {
    memberProjectionStore.clear()
  })

  it('returns all four projections, undefined until seeded', () => {
    const { result } = renderHook(() => useMemberProjections('a'))
    expect(result.current).toEqual({
      roster: undefined,
      activity: undefined,
      wake: undefined,
      driving: undefined,
    })
    act(() => {
      memberProjectionStore.apply('a', 'roster', { name: 'A', slug: 'a' }, 1)
      memberProjectionStore.apply('a', 'driving', { open: ['s1'] }, 1)
    })
    expect(result.current.roster).toEqual({ name: 'A', slug: 'a' })
    expect(result.current.driving).toEqual({ open: ['s1'] })
    expect(result.current.activity).toBeUndefined()
  })
})

describe('useMemberRosterViews', () => {
  beforeEach(() => {
    memberProjectionStore.clear()
  })

  it('builds a Map over every slug, undefined until seeded', () => {
    const { result } = renderHook(() => useMemberRosterViews(['a', 'b']))
    expect(result.current.get('a')).toBeUndefined()
    expect(result.current.get('b')).toBeUndefined()
  })

  it('changes Map identity once when a subscribed slug frame lands', () => {
    const { result } = renderHook(() => useMemberRosterViews(['a', 'b']))
    const before = result.current
    act(() => { memberProjectionStore.apply('a', 'roster', { name: 'A', slug: 'a', starred: true }, 2) })
    const after = result.current
    expect(after).not.toBe(before)
    expect(after.get('a')).toEqual({ name: 'A', slug: 'a', starred: true })
    expect(after.get('b')).toBeUndefined()
  })

  it('does not change Map identity for an unrelated key or slug', () => {
    const { result } = renderHook(() => useMemberRosterViews(['a', 'b']))
    const before = result.current
    act(() => {
      // A different KEY on a subscribed slug.
      memberProjectionStore.apply('a', 'wake', { patrol: 'armed' }, 1)
      // A different SLUG entirely.
      memberProjectionStore.apply('z', 'roster', { name: 'Z', slug: 'z' }, 1)
    })
    expect(result.current).toBe(before)
  })
})
