/**
 * The "Translucent panels" display setting (opt-in; the Liquid Glass primitive):
 * the storage key, the root
 * attribute, and the hook that binds the two.
 *
 * One module owns the key name and the attribute value (index.html reads the
 * same key before hydration), so these tests pin the contract the bootstrap
 * and index.css's mirror block rely on: `mc-liquid-glass` === `'on'` <->
 * `data-reduce-transparency="off"`, and anything else -- absent key, other
 * value, blocked storage -- <-> `data-reduce-transparency="on"` (solid). The
 * default is the solid rendering; the effect is what the user turns on.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { act, render, renderHook } from '@testing-library/react'

import {
  LIQUID_GLASS_STORAGE_KEY,
  applyLiquidGlass,
  persistLiquidGlass,
  readLiquidGlass,
} from '../utils/liquidGlass'
import { useLiquidGlass } from '../hooks/useLiquidGlass'
import { TranslucentPanelsPreview } from '../pages/settings/TranslucentPanelsPreview'

function blockStorage(): () => void {
  const original = Object.getOwnPropertyDescriptor(window, 'localStorage')
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    get() { throw new DOMException('blocked', 'SecurityError') },
  })
  return () => { if (original) Object.defineProperty(window, 'localStorage', original) }
}

afterEach(() => {
  localStorage.removeItem(LIQUID_GLASS_STORAGE_KEY)
  document.documentElement.removeAttribute('data-reduce-transparency')
})

describe('liquidGlass utils', () => {
  it('reads on only for the exact stored value the bootstrap checks; absent is off', () => {
    expect(LIQUID_GLASS_STORAGE_KEY).toBe('mc-liquid-glass')
    expect(readLiquidGlass()).toBe(false)
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    expect(readLiquidGlass()).toBe(true)
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'true')
    expect(readLiquidGlass()).toBe(false)
  })

  it('persists on as the key and off as its absence', () => {
    persistLiquidGlass(true)
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')
    persistLiquidGlass(false)
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
  })

  it('writes the solidifying attribute index.css keys its mirror block on, inverted', () => {
    applyLiquidGlass(true)
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
    applyLiquidGlass(false)
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
  })

  it('degrades to off (solid) and does not throw when storage is blocked', () => {
    const restore = blockStorage()
    try {
      expect(readLiquidGlass()).toBe(false)
      expect(() => persistLiquidGlass(true)).not.toThrow()
      expect(() => persistLiquidGlass(false)).not.toThrow()
    } finally {
      restore()
    }
  })
})

describe('useLiquidGlass', () => {
  it('starts solid on a fresh store and takes over the root attribute on mount', () => {
    const { result } = renderHook(() => useLiquidGlass())
    expect(result.current.liquidGlass).toBe(false)
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
  })

  it('starts from storage when the user turned glass on', () => {
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    const { result } = renderHook(() => useLiquidGlass())
    expect(result.current.liquidGlass).toBe(true)
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
  })

  it('flips the attribute and the stored key together, both ways', () => {
    const { result } = renderHook(() => useLiquidGlass())

    act(() => { result.current.setLiquidGlass(true) })
    expect(result.current.liquidGlass).toBe(true)
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')

    act(() => { result.current.setLiquidGlass(false) })
    expect(result.current.liquidGlass).toBe(false)
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
  })

  it('keeps the setter identity across re-renders', () => {
    const { result, rerender } = renderHook(() => useLiquidGlass())
    const first = result.current.setLiquidGlass
    rerender()
    expect(result.current.setLiquidGlass).toBe(first)
  })
})

describe('TranslucentPanelsPreview', () => {
  // The preview under the switch is the REAL primitive (so it follows the
  // switch through the same root attribute the app does), decorative, and
  // wordless apart from the composer placeholder it is handed.
  it('renders a real Glass dock and chips, hidden from AT and inert', () => {
    const { getByTestId } = render(<TranslucentPanelsPreview placeholder="Message Kiro…" />)
    const root = getByTestId('translucent-panels-preview')
    expect(root.getAttribute('aria-hidden')).toBe('true')
    expect(root.className).toContain('pointer-events-none')
    expect(root.querySelectorAll('.liquid-glass').length).toBe(3)
    expect(root.querySelector('.liquid-glass.glass-shadow')).not.toBeNull()
    expect(root.querySelectorAll('button, input, textarea, a, [tabindex]').length).toBe(0)
    expect(root.textContent).toBe('Message Kiro…')
  })
})
