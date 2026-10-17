/**
 * useEdgeHide — the companion's edge dock/peek state. Docking works when this hook's
 * `setIsPeeking` updates `isPeekingRef` SYNCHRONOUSLY: useDrag reads that ref inside
 * its imperative mouse handlers to decide whether to un-dock as a drag begins, so a
 * render-lagged ref (the old inline `isPeekingRef.current = isPeeking`) is exactly why
 * the dock state was inconsistent. These pin that contract and the edge domain the
 * dock transform maps to a crop direction.
 */
import { describe, it, expect } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useEdgeHide } from '../apps/crew-companion/useEdgeHide'

describe('useEdgeHide initial state', () => {
  it('starts undocked: no edge, not peeking, ref false', () => {
    const { result } = renderHook(() => useEdgeHide())
    expect(result.current.hideEdge).toBe(null)
    expect(result.current.isPeeking).toBe(false)
    expect(result.current.isPeekingRef.current).toBe(false)
  })
})

describe('useEdgeHide synchronous ref (the piece that makes docking work)', () => {
  it('updates isPeekingRef IN THE SAME CALL, before any re-render flush', () => {
    const { result } = renderHook(() => useEdgeHide())
    // The ref object is stable across renders, so a synchronous write is visible
    // immediately — this is what useDrag depends on.
    const ref = result.current.isPeekingRef
    act(() => {
      result.current.setIsPeeking(true)
      // Read the ref WITHIN the act, before React commits the state update.
      expect(ref.current).toBe(true)
    })
    // And the state has settled to match after the flush.
    expect(result.current.isPeeking).toBe(true)
    expect(result.current.isPeekingRef.current).toBe(true)
  })

  it('clears the ref synchronously too', () => {
    const { result } = renderHook(() => useEdgeHide())
    act(() => { result.current.setIsPeeking(true) })
    const ref = result.current.isPeekingRef
    act(() => {
      result.current.setIsPeeking(false)
      expect(ref.current).toBe(false)
    })
    expect(result.current.isPeeking).toBe(false)
  })
})

describe('useEdgeHide edge domain', () => {
  it('tracks each dock edge and clears back to the open middle', () => {
    const { result } = renderHook(() => useEdgeHide())
    act(() => { result.current.setHideEdge('left') })
    expect(result.current.hideEdge).toBe('left')
    act(() => { result.current.setHideEdge('right') })
    expect(result.current.hideEdge).toBe('right')
    act(() => { result.current.setHideEdge(null) })
    expect(result.current.hideEdge).toBe(null)
  })

  it('a docked companion is exactly (isPeeking AND an edge) — what pet.tsx renders', () => {
    const { result } = renderHook(() => useEdgeHide())
    const docked = () => result.current.isPeeking && result.current.hideEdge !== null
    expect(docked()).toBe(false)
    act(() => { result.current.setHideEdge('left'); result.current.setIsPeeking(true) })
    expect(docked()).toBe(true)
    // Standing up (a drag/walk begins) drops both.
    act(() => { result.current.setIsPeeking(false); result.current.setHideEdge(null) })
    expect(docked()).toBe(false)
  })
})
