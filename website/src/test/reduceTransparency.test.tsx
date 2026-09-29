/**
 * The "Reduce glass transparency" display setting: the storage key, the root
 * attribute, and the hook that binds the two.
 *
 * One module owns the key name and the attribute value (index.html reads the
 * same key before hydration), so these tests pin the contract the bootstrap
 * and index.css's mirror block rely on: `mc-reduce-transparency` === `'on'`
 * <-> `data-reduce-transparency="on"`. Storage may be unavailable (a blocked
 * third-party embed, private mode) -- the setting must then degrade to off /
 * session-only rather than throw during render.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'

import {
  REDUCE_TRANSPARENCY_STORAGE_KEY,
  applyReduceTransparency,
  persistReduceTransparency,
  readReduceTransparency,
} from '../utils/reduceTransparency'
import { useReduceTransparency } from '../hooks/useReduceTransparency'

function blockStorage(): () => void {
  const original = Object.getOwnPropertyDescriptor(window, 'localStorage')
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get() { throw new DOMException('blocked', 'SecurityError') },
  })
  return () => { if (original) Object.defineProperty(window, 'localStorage', original) }
}

afterEach(() => {
  localStorage.removeItem(REDUCE_TRANSPARENCY_STORAGE_KEY)
  document.documentElement.removeAttribute('data-reduce-transparency')
})

describe('reduceTransparency utils', () => {
  it('reads on only for the exact stored value the bootstrap checks', () => {
    expect(REDUCE_TRANSPARENCY_STORAGE_KEY).toBe('mc-reduce-transparency')
    expect(readReduceTransparency()).toBe(false)
    localStorage.setItem(REDUCE_TRANSPARENCY_STORAGE_KEY, 'on')
    expect(readReduceTransparency()).toBe(true)
    localStorage.setItem(REDUCE_TRANSPARENCY_STORAGE_KEY, 'true')
    expect(readReduceTransparency()).toBe(false)
  })

  it('persists on as the key and off as its absence', () => {
    persistReduceTransparency(true)
    expect(localStorage.getItem(REDUCE_TRANSPARENCY_STORAGE_KEY)).toBe('on')
    persistReduceTransparency(false)
    expect(localStorage.getItem(REDUCE_TRANSPARENCY_STORAGE_KEY)).toBeNull()
  })

  it('writes the root attribute index.css keys its mirror block on', () => {
    applyReduceTransparency(true)
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
    applyReduceTransparency(false)
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
  })

  it('degrades to off and does not throw when storage is blocked', () => {
    const restore = blockStorage()
    try {
      expect(readReduceTransparency()).toBe(false)
      expect(() => persistReduceTransparency(true)).not.toThrow()
      expect(() => persistReduceTransparency(false)).not.toThrow()
    } finally {
      restore()
    }
  })
})

describe('useReduceTransparency', () => {
  it('starts from storage and takes over the root attribute on mount', () => {
    localStorage.setItem(REDUCE_TRANSPARENCY_STORAGE_KEY, 'on')
    const { result } = renderHook(() => useReduceTransparency())
    expect(result.current.reduceTransparency).toBe(true)
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
  })

  it('flips the attribute and the stored key together, both ways', () => {
    const { result } = renderHook(() => useReduceTransparency())
    expect(result.current.reduceTransparency).toBe(false)
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')

    act(() => { result.current.setReduceTransparency(true) })
    expect(result.current.reduceTransparency).toBe(true)
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
    expect(localStorage.getItem(REDUCE_TRANSPARENCY_STORAGE_KEY)).toBe('on')

    act(() => { result.current.setReduceTransparency(false) })
    expect(result.current.reduceTransparency).toBe(false)
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
    expect(localStorage.getItem(REDUCE_TRANSPARENCY_STORAGE_KEY)).toBeNull()
  })

  it('keeps the setter identity across re-renders', () => {
    const { result, rerender } = renderHook(() => useReduceTransparency())
    const first = result.current.setReduceTransparency
    rerender()
    expect(result.current.setReduceTransparency).toBe(first)
  })
})
