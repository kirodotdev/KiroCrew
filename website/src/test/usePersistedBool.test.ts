/**
 * Tests for usePersistedBool — localStorage-backed boolean view preferences
 * (word wrap, line numbers, diff split/unified, capsule collapse, …).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePersistedBool } from '../hooks/usePersistedBool'
import { safeSetItem } from '../utils/safeStorage'

describe('usePersistedBool', () => {
  beforeEach(() => localStorage.clear())

  it('falls back to the default when nothing is persisted', () => {
    const { result } = renderHook(() => usePersistedBool('t-key', true))
    expect(result.current[0]).toBe(true)
  })

  it('reads a persisted "1"/"0" over the default on mount', () => {
    safeSetItem('t-key', '0')
    const { result } = renderHook(() => usePersistedBool('t-key', true))
    expect(result.current[0]).toBe(false)

    safeSetItem('t-key2', '1')
    const { result: r2 } = renderHook(() => usePersistedBool('t-key2', false))
    expect(r2.current[0]).toBe(true)
  })

  it('writes changes back to localStorage', () => {
    const { result } = renderHook(() => usePersistedBool('t-key', false))
    act(() => result.current[1](true))
    expect(result.current[0]).toBe(true)
    expect(localStorage.getItem('t-key')).toBe('1')
    act(() => result.current[1](false))
    expect(localStorage.getItem('t-key')).toBe('0')
  })

  it('a fresh mount picks up the value a previous instance persisted', () => {
    const first = renderHook(() => usePersistedBool('t-shared', false))
    act(() => first.result.current[1](true))
    first.unmount()
    const second = renderHook(() => usePersistedBool('t-shared', false))
    expect(second.result.current[0]).toBe(true)
  })

  it('supports functional updates', () => {
    const { result } = renderHook(() => usePersistedBool('t-fn', false))
    act(() => result.current[1](v => !v))
    expect(result.current[0]).toBe(true)
    expect(localStorage.getItem('t-fn')).toBe('1')
  })

  describe('same-key live sync', () => {
    it('toggling one mounted instance updates a same-key sibling synchronously', () => {
      const a = renderHook(() => usePersistedBool('t-live', false))
      const b = renderHook(() => usePersistedBool('t-live', false))
      act(() => a.result.current[1](true))
      expect(a.result.current[0]).toBe(true)
      expect(b.result.current[0]).toBe(true)
      act(() => b.result.current[1](false))
      expect(a.result.current[0]).toBe(false)
      expect(b.result.current[0]).toBe(false)
      expect(localStorage.getItem('t-live')).toBe('0')
    })

    it('functional updates propagate to same-key siblings', () => {
      const a = renderHook(() => usePersistedBool('t-live-fn', false))
      const b = renderHook(() => usePersistedBool('t-live-fn', false))
      act(() => a.result.current[1](v => !v))
      expect(b.result.current[0]).toBe(true)
    })

    it('different keys do not cross-talk', () => {
      const a = renderHook(() => usePersistedBool('t-key-a', false))
      const b = renderHook(() => usePersistedBool('t-key-b', false))
      act(() => a.result.current[1](true))
      expect(a.result.current[0]).toBe(true)
      expect(b.result.current[0]).toBe(false)
      expect(localStorage.getItem('t-key-b')).toBe('0')
    })

    it('a storage event for the key updates a mounted hook (cross-tab path)', () => {
      const { result } = renderHook(() => usePersistedBool('t-xtab', false))
      act(() => {
        window.dispatchEvent(new StorageEvent('storage', { key: 't-xtab', newValue: '1' }))
      })
      expect(result.current[0]).toBe(true)
      // Anything other than '1' parses false, same as the mount read.
      act(() => {
        window.dispatchEvent(new StorageEvent('storage', { key: 't-xtab', newValue: '0' }))
      })
      expect(result.current[0]).toBe(false)
    })

    it('a storage event with a null newValue falls back to the default (key removed)', () => {
      const { result } = renderHook(() => usePersistedBool('t-xtab-null', true))
      act(() => {
        window.dispatchEvent(new StorageEvent('storage', { key: 't-xtab-null', newValue: '0' }))
      })
      expect(result.current[0]).toBe(false)
      act(() => {
        window.dispatchEvent(new StorageEvent('storage', { key: 't-xtab-null', newValue: null }))
      })
      expect(result.current[0]).toBe(true)
    })

    it('a storage event for a different key is ignored', () => {
      const { result } = renderHook(() => usePersistedBool('t-xtab-mine', false))
      act(() => {
        window.dispatchEvent(new StorageEvent('storage', { key: 't-xtab-other', newValue: '1' }))
      })
      expect(result.current[0]).toBe(false)
    })

    it('a storage event from a different storage area is ignored', () => {
      const { result } = renderHook(() => usePersistedBool('t-xtab-area', false))
      act(() => {
        window.dispatchEvent(
          new StorageEvent('storage', {
            key: 't-xtab-area',
            newValue: '1',
            storageArea: sessionStorage,
          }),
        )
      })
      expect(result.current[0]).toBe(false)
      // The same event scoped to localStorage IS honored.
      act(() => {
        window.dispatchEvent(
          new StorageEvent('storage', {
            key: 't-xtab-area',
            newValue: '1',
            storageArea: localStorage,
          }),
        )
      })
      expect(result.current[0]).toBe(true)
    })

    it('a setter write broadcasts exactly once (no echo loop between siblings)', () => {
      const a = renderHook(() => usePersistedBool('t-echo', false))
      renderHook(() => usePersistedBool('t-echo', false))
      const seen: unknown[] = []
      const spy = (e: Event) => seen.push((e as CustomEvent).detail)
      window.addEventListener('mc:persisted-bool', spy)
      try {
        act(() => a.result.current[1](true))
        expect(seen).toEqual([{ key: 't-echo', value: true }])
      } finally {
        window.removeEventListener('mc:persisted-bool', spy)
      }
    })

    it('mounting does not broadcast the initial value', () => {
      const seen: unknown[] = []
      const spy = (e: Event) => seen.push((e as CustomEvent).detail)
      window.addEventListener('mc:persisted-bool', spy)
      try {
        renderHook(() => usePersistedBool('t-mount-quiet', true))
        expect(seen).toEqual([])
      } finally {
        window.removeEventListener('mc:persisted-bool', spy)
      }
    })

    it('unmount removes both listeners; no update reaches an unmounted hook', () => {
      const addSpy = vi.spyOn(window, 'addEventListener')
      const removeSpy = vi.spyOn(window, 'removeEventListener')
      const { unmount } = renderHook(() => usePersistedBool('t-cleanup', false))
      const added = addSpy.mock.calls.filter(
        ([type]) => type === 'storage' || type === 'mc:persisted-bool',
      )
      // >= 2 (not an exact count) so a future StrictMode test wrapper doesn't
      // break this pin; the identity check below is what proves cleanup.
      expect(added.length).toBeGreaterThanOrEqual(2)
      unmount()
      const removed = removeSpy.mock.calls.filter(
        ([type]) => type === 'storage' || type === 'mc:persisted-bool',
      )
      expect(removed.length).toBeGreaterThanOrEqual(2)
      // Same listener references were detached, so post-unmount events are inert.
      expect(removed.map(c => c[1])).toEqual(expect.arrayContaining(added.map(c => c[1])))
      const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
      try {
        act(() => {
          window.dispatchEvent(new StorageEvent('storage', { key: 't-cleanup', newValue: '1' }))
          window.dispatchEvent(
            new CustomEvent('mc:persisted-bool', { detail: { key: 't-cleanup', value: true } }),
          )
        })
        expect(errSpy).not.toHaveBeenCalled()
      } finally {
        errSpy.mockRestore()
        addSpy.mockRestore()
        removeSpy.mockRestore()
      }
    })

    it('a malformed sync event detail is ignored', () => {
      const { result } = renderHook(() => usePersistedBool('t-malformed', false))
      act(() => {
        window.dispatchEvent(new CustomEvent('mc:persisted-bool'))
        window.dispatchEvent(
          new CustomEvent('mc:persisted-bool', { detail: { key: 't-malformed', value: 'yes' } }),
        )
      })
      expect(result.current[0]).toBe(false)
    })

    it('a sync-received value is not re-persisted by the receiver', () => {
      // Only the originating instance writes localStorage; a receiver writing
      // back would fire a fresh cross-tab storage event at the origin and two
      // queued alternating events could ping-pong forever (GPT review finding).
      const { result } = renderHook(() => usePersistedBool('t-norepersist', false))
      expect(localStorage.getItem('t-norepersist')).toBe('0') // mount write intact
      act(() => {
        window.dispatchEvent(
          new CustomEvent('mc:persisted-bool', { detail: { key: 't-norepersist', value: true } }),
        )
      })
      expect(result.current[0]).toBe(true)
      expect(localStorage.getItem('t-norepersist')).toBe('0') // receiver did not write
    })

    it('a cross-tab key removal sticks (not resurrected by the receiver)', () => {
      const { result } = renderHook(() => usePersistedBool('t-removal', true))
      act(() => result.current[1](false))
      expect(localStorage.getItem('t-removal')).toBe('0')
      act(() => {
        localStorage.removeItem('t-removal') // what the other tab's removal does
        window.dispatchEvent(new StorageEvent('storage', { key: 't-removal', newValue: null }))
      })
      expect(result.current[0]).toBe(true) // back to the default
      expect(localStorage.getItem('t-removal')).toBe(null) // key stays removed
    })
  })
})
