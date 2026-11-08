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
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { act, render, renderHook } from '@testing-library/react'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: { ...mod.api, updateThemeConfig: vi.fn().mockResolvedValue({}) },
  }
})

import { api } from '../api/client'
import {
  LIQUID_GLASS_CHANGED_EVENT,
  LIQUID_GLASS_SEEDED_KEY,
  LIQUID_GLASS_STORAGE_KEY,
  adoptServerLiquidGlass,
  applyLiquidGlass,
  liquidGlassSeeded,
  markLiquidGlassSeeded,
  persistLiquidGlass,
  readLiquidGlass,
  toTranslucentPanelsStored,
} from '../utils/liquidGlass'
import { useLiquidGlass } from '../hooks/useLiquidGlass'
import { TranslucentPanelsPreview } from '../pages/settings/TranslucentPanelsPreview'

const updateThemeConfig = vi.mocked(api.updateThemeConfig)
beforeEach(() => { updateThemeConfig.mockClear() })

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
  localStorage.removeItem(LIQUID_GLASS_SEEDED_KEY)
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

  it('the seeded marker is its own key, absent until marked, and reads as seeded when the store is blocked', () => {
    expect(LIQUID_GLASS_SEEDED_KEY).toBe('mc-liquid-glass-seeded')
    expect(liquidGlassSeeded()).toBe(false)
    markLiquidGlassSeeded()
    expect(liquidGlassSeeded()).toBe(true)
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull() // the marker never touches the setting
    const restore = blockStorage()
    try {
      expect(liquidGlassSeeded()).toBe(true)
      expect(() => markLiquidGlassSeeded()).not.toThrow()
    } finally {
      restore()
    }
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

  it('maps the switch onto the two stored workspace values the gateway accepts', () => {
    expect(toTranslucentPanelsStored(true)).toBe('on')
    expect(toTranslucentPanelsStored(false)).toBe('off')
  })
})

describe('adoptServerLiquidGlass (the boot read of dashboard.translucent_panels)', () => {
  // The workspace is the source of truth and the browser the render cache, as
  // for mc-theme: a browser-only store is per origin, so a new gateway port or
  // a second browser read as "the glass turned itself off".
  it("'on' turns a fresh browser's glass on: key, attribute and the change event", () => {
    const seen = vi.fn()
    window.addEventListener(LIQUID_GLASS_CHANGED_EVENT, seen)
    try {
      expect(adoptServerLiquidGlass('on')).toBe(true)
      expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')
      expect(document.documentElement.dataset.reduceTransparency).toBe('off')
      expect(seen).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(LIQUID_GLASS_CHANGED_EVENT, seen)
    }
  })

  it("'off' turns a browser that had it on back off", () => {
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    expect(adoptServerLiquidGlass('off')).toBe(true)
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
  })

  it('a value the browser already holds changes nothing and fires no event', () => {
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    const seen = vi.fn()
    window.addEventListener(LIQUID_GLASS_CHANGED_EVENT, seen)
    try {
      expect(adoptServerLiquidGlass('on')).toBe(false)
      expect(seen).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(LIQUID_GLASS_CHANGED_EVENT, seen)
    }
  })

  it('an explicit workspace value marks the first-run default as offered, even when nothing changed', () => {
    // A fresh browser whose cache already reads off adopts 'off' unchanged;
    // without the marker the unfinished tour would seed the glass on and
    // publish that over the workspace's 'off'.
    expect(liquidGlassSeeded()).toBe(false)
    expect(adoptServerLiquidGlass('off')).toBe(false)
    expect(liquidGlassSeeded()).toBe(true)
    localStorage.removeItem(LIQUID_GLASS_SEEDED_KEY)
    expect(adoptServerLiquidGlass('on')).toBe(true)
    expect(liquidGlassSeeded()).toBe(true)
  })

  it("'' leaves the first-run default on offer: the workspace has no choice yet", () => {
    expect(adoptServerLiquidGlass('')).toBe(false)
    expect(liquidGlassSeeded()).toBe(false)
  })

  it("'' (a workspace that predates the key) and junk leave the browser copy alone, both ways", () => {
    // An existing user's ON must survive the upgrade that made the setting
    // workspace-backed: '' is "never chosen here", never "off".
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    for (const stored of ['', undefined, null, true, 'yes']) {
      expect(adoptServerLiquidGlass(stored)).toBe(false)
      expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')
    }
    localStorage.removeItem(LIQUID_GLASS_STORAGE_KEY)
    for (const stored of ['', undefined, false]) {
      expect(adoptServerLiquidGlass(stored)).toBe(false)
      expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBeNull()
    }
    expect(document.documentElement.dataset.reduceTransparency).toBeUndefined()
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

  it('publishes every change to the workspace through PUT /api/config/theme', async () => {
    const { result } = renderHook(() => useLiquidGlass())
    await act(async () => { result.current.setLiquidGlass(true) })
    expect(updateThemeConfig).toHaveBeenLastCalledWith({ translucent_panels: 'on' })
    await act(async () => { result.current.setLiquidGlass(false) })
    expect(updateThemeConfig).toHaveBeenLastCalledWith({ translucent_panels: 'off' })
    expect(updateThemeConfig).toHaveBeenCalledTimes(2)
  })

  it('a publish that throws (an api without the endpoint) cannot fail the flip', async () => {
    // The corpus mocks `api` with bare factories; the real app always has the
    // endpoint. Either way the write is off the flip's own call stack.
    updateThemeConfig.mockImplementationOnce(() => { throw new TypeError('not a function') })
    const { result } = renderHook(() => useLiquidGlass())
    await act(async () => { result.current.setLiquidGlass(true) })
    expect(result.current.liquidGlass).toBe(true)
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
  })

  it('a failed publish keeps the browser copy and the attribute for this session', async () => {
    updateThemeConfig.mockRejectedValueOnce(new Error('offline'))
    const { result } = renderHook(() => useLiquidGlass())
    act(() => { result.current.setLiquidGlass(true) })
    await act(async () => { await Promise.resolve() })
    expect(result.current.liquidGlass).toBe(true)
    expect(localStorage.getItem(LIQUID_GLASS_STORAGE_KEY)).toBe('on')
    expect(document.documentElement.dataset.reduceTransparency).toBe('off')
  })

  it('every mounted instance follows the store: the Settings row and the first-run row agree', () => {
    const a = renderHook(() => useLiquidGlass())
    const b = renderHook(() => useLiquidGlass())
    act(() => { a.result.current.setLiquidGlass(true) })
    expect(b.result.current.liquidGlass).toBe(true)
    // The boot adoption of the workspace value reaches mounted hooks the same way.
    act(() => { adoptServerLiquidGlass('off') })
    expect(a.result.current.liquidGlass).toBe(false)
    expect(b.result.current.liquidGlass).toBe(false)
    expect(document.documentElement.dataset.reduceTransparency).toBe('on')
  })

  it('with site storage blocked the switch still turns on for this session, in every mounted instance', async () => {
    // persistLiquidGlass cannot write, so a listener that re-read the store
    // would answer false and undo the flip; the value rides in the event.
    const a = renderHook(() => useLiquidGlass())
    const b = renderHook(() => useLiquidGlass())
    const restore = blockStorage()
    try {
      await act(async () => { a.result.current.setLiquidGlass(true) })
      expect(a.result.current.liquidGlass).toBe(true)
      expect(b.result.current.liquidGlass).toBe(true)
      expect(document.documentElement.dataset.reduceTransparency).toBe('off')
      expect(updateThemeConfig).toHaveBeenLastCalledWith({ translucent_panels: 'on' })
      act(() => { a.result.current.setLiquidGlass(false) })
      expect(b.result.current.liquidGlass).toBe(false)
      expect(document.documentElement.dataset.reduceTransparency).toBe('on')
    } finally {
      restore()
    }
  })

  it('a foreign dispatch of the event with no value falls back to the store', () => {
    const { result } = renderHook(() => useLiquidGlass())
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    act(() => { window.dispatchEvent(new Event(LIQUID_GLASS_CHANGED_EVENT)) })
    expect(result.current.liquidGlass).toBe(true)
  })

  it('a mount does not publish: only a change does', () => {
    localStorage.setItem(LIQUID_GLASS_STORAGE_KEY, 'on')
    renderHook(() => useLiquidGlass())
    expect(updateThemeConfig).not.toHaveBeenCalled()
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
