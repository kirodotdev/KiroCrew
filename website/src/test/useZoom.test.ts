import { test, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useZoom } from '../hooks/useZoom'

// The hook talks to the native zoom bridge (window.zoomAPI, injected by
// electron/preload.js). Tests install a mock bridge to simulate the desktop
// app; deleting it simulates a plain-browser dashboard.
type ZoomAPI = { get: () => Promise<number>; set: (f: number) => Promise<number>; step: (d: 1 | -1) => Promise<number> }

function installZoomAPI(initial = 1) {
  let factor = initial
  const api = {
    get: vi.fn(() => Promise.resolve(factor)),
    set: vi.fn((f: number) => { factor = f; return Promise.resolve(factor) }),
    // Mirror the main-process ladder loosely: ±10 points is enough for the hook's contract.
    step: vi.fn((d: 1 | -1) => { factor = Math.round((factor + d * 0.1) * 100) / 100; return Promise.resolve(factor) }),
  }
  ;(window as unknown as { zoomAPI?: ZoomAPI }).zoomAPI = api
  return api
}

beforeEach(() => localStorage.clear())
afterEach(() => { delete (window as unknown as { zoomAPI?: ZoomAPI }).zoomAPI })

// ── plain browser (no bridge) ──

test('browser: zoom is unsupported and controls are safe no-ops', () => {
  const { result } = renderHook(() => useZoom())
  expect(result.current.zoomSupported).toBe(false)
  expect(result.current.zoom).toBe(100)
  act(() => result.current.zoomIn())
  act(() => result.current.zoomOut())
  act(() => result.current.reset())
  expect(result.current.zoom).toBe(100)
})

test('browser: legacy page-scaling keys are removed without a bridge', () => {
  localStorage.setItem('mc-zoom', '120')
  localStorage.setItem('mc-font-scale', '130')
  renderHook(() => useZoom())
  expect(localStorage.getItem('mc-zoom')).toBeNull()
  expect(localStorage.getItem('mc-font-scale')).toBeNull()
})

test('browser: never applies page-side scaling (no CSS zoom, no html font-size)', () => {
  const root = document.createElement('div')
  root.id = 'root'
  document.body.appendChild(root)
  const setSpy = vi.spyOn(root.style, 'setProperty')
  try {
    localStorage.setItem('mc-zoom', '120')
    localStorage.setItem('mc-font-scale', '150')
    renderHook(() => useZoom())
    expect(setSpy).not.toHaveBeenCalledWith('zoom', expect.anything())
    expect(document.documentElement.style.fontSize).toBe('')
  } finally {
    setSpy.mockRestore()
    root.remove()
  }
})

// ── desktop (bridge present) ──

test('desktop: reads the native factor on mount', async () => {
  const api = installZoomAPI(1.25)
  const { result } = renderHook(() => useZoom())
  expect(result.current.zoomSupported).toBe(true)
  await act(async () => {})
  expect(api.get).toHaveBeenCalled()
  expect(result.current.zoom).toBe(125)
})

test('desktop: zoomIn/zoomOut step through the bridge and reflect the applied factor', async () => {
  const api = installZoomAPI(1)
  const { result } = renderHook(() => useZoom())
  await act(async () => { result.current.zoomIn() })
  expect(api.step).toHaveBeenCalledWith(1)
  expect(result.current.zoom).toBe(110)
  await act(async () => { result.current.zoomOut() })
  expect(api.step).toHaveBeenCalledWith(-1)
  expect(result.current.zoom).toBe(100)
})

test('desktop: reset sets the native factor to 1', async () => {
  const api = installZoomAPI(1.5)
  const { result } = renderHook(() => useZoom())
  await act(async () => {})
  expect(result.current.zoom).toBe(150)
  await act(async () => { result.current.reset() })
  expect(api.set).toHaveBeenCalledWith(1)
  expect(result.current.zoom).toBe(100)
})

test('desktop: window resize re-syncs from the bridge (covers menu/ctrl+wheel zoom)', async () => {
  const api = installZoomAPI(1)
  const { result } = renderHook(() => useZoom())
  await act(async () => {})
  expect(result.current.zoom).toBe(100)
  // Zoom changed outside the hook (e.g. Cmd+= via the View menu): the
  // viewport resize is the renderer-visible signal.
  await act(async () => { await api.set(1.5) })
  api.set.mockClear()
  await act(async () => { window.dispatchEvent(new Event('resize')) })
  expect(result.current.zoom).toBe(150)
})

test('desktop: legacy zoom × font-scale folds into the native factor once', async () => {
  localStorage.setItem('mc-zoom', '120')
  localStorage.setItem('mc-font-scale', '130')
  const api = installZoomAPI(1)
  renderHook(() => useZoom())
  await act(async () => {})
  expect(api.set).toHaveBeenCalledWith(1.2 * 1.3)
  expect(localStorage.getItem('mc-zoom')).toBeNull()
  expect(localStorage.getItem('mc-font-scale')).toBeNull()
})

test('desktop: legacy values at 100% migrate silently (keys removed, no set call)', async () => {
  localStorage.setItem('mc-zoom', '100')
  localStorage.setItem('mc-font-scale', '100')
  const api = installZoomAPI(1)
  renderHook(() => useZoom())
  await act(async () => {})
  expect(api.set).not.toHaveBeenCalled()
  expect(localStorage.getItem('mc-zoom')).toBeNull()
  expect(localStorage.getItem('mc-font-scale')).toBeNull()
})

test('desktop: oversized legacy combination clamps to the 300% ceiling', async () => {
  localStorage.setItem('mc-zoom', '150')
  localStorage.setItem('mc-font-scale', '250')
  const api = installZoomAPI(1)
  renderHook(() => useZoom())
  await act(async () => {})
  expect(api.set).toHaveBeenCalledWith(3)
})

test('desktop: no legacy keys means no migration set call', async () => {
  const api = installZoomAPI(1)
  renderHook(() => useZoom())
  await act(async () => {})
  expect(api.set).not.toHaveBeenCalled()
})

// ── font family (unchanged behavior) ──

test('defaults font family to sans', () => {
  const { result } = renderHook(() => useZoom())
  expect(result.current.family).toBe('sans')
})

test('setFontFamily updates state and persists', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.setFontFamily('mono'))
  expect(result.current.family).toBe('mono')
  expect(localStorage.getItem('mc-font-family')).toBe('mono')
})

test('publishes the resolved family on html[data-font-family]', () => {
  // CSS keys off this attribute to compensate for JetBrains Mono being ~20%
  // wider than Space Grotesk — see the html[data-font-family="mono"] rule in
  // index.css. Without the attribute the nav rail's community row truncates for
  // mono users, and that failure is INVISIBLE on the default family, so it is
  // pinned here rather than left to manual checking.
  const { result } = renderHook(() => useZoom())
  expect(document.documentElement.dataset.fontFamily).toBe('sans')
  act(() => result.current.setFontFamily('mono'))
  expect(document.documentElement.dataset.fontFamily).toBe('mono')
  act(() => result.current.setFontFamily('system'))
  expect(document.documentElement.dataset.fontFamily).toBe('system')
})

test('routes Sans and Mono through the theme role tokens, System through neither', () => {
  // An installed theme pack fills --theme-font-sans / --theme-font-mono. The
  // preference writes --font-body as an INLINE style on <html>, which outranks
  // every selector — so it must READ the tokens rather than hardcode a stack, or
  // a pack's font is unreachable no matter what it declares. System is the one
  // option that must never read a token: the OS face is not the theme's to take.
  const { result } = renderHook(() => useZoom())
  const readBody = () => document.documentElement.style.getPropertyValue('--font-body')

  act(() => result.current.setFontFamily('sans'))
  expect(readBody()).toContain('var(--theme-font-sans,')
  expect(readBody()).toContain("'Space Grotesk'")

  act(() => result.current.setFontFamily('mono'))
  expect(readBody()).toContain('var(--theme-font-mono,')
  expect(readBody()).toContain("'JetBrains Mono'")

  act(() => result.current.setFontFamily('system'))
  expect(readBody()).not.toContain('--theme-font')
  expect(readBody()).toContain('-apple-system')
})

test('cycleFamily rotates sans → mono → system → opendyslexic → sans', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('mono')
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('system')
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('opendyslexic')
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('sans')
})

// ── OpenDyslexic (accessibility built-in) ──
// A dedicated Font Family option that ships bundled proportional + monospace
// faces (see index.css @font-face blocks). Distinct from Sans/Mono/System in two
// ways: it does NOT read theme role tokens (a theme pack cannot hijack the a11y
// font), and it publishes its own --mono override so code blocks also render in
// OpenDyslexicMono.

test('opendyslexic sets --font-body to the bundled OpenDyslexic stack', () => {
  const { result } = renderHook(() => useZoom())
  const readBody = () => document.documentElement.style.getPropertyValue('--font-body')

  act(() => result.current.setFontFamily('opendyslexic'))
  expect(readBody()).toContain("'OpenDyslexic'")
  expect(readBody()).toContain('sans-serif')
  // Deliberately NOT routed through --theme-font-* — the a11y font must reach
  // the user regardless of the installed theme pack's declared faces.
  expect(readBody()).not.toContain('--theme-font')
})

test('opendyslexic publishes data-font-family="opendyslexic" on <html>', () => {
  // The attribute survives with the user's actual choice — not auto-resolved to
  // "mono" even in CLI mode. This is asymmetric with the sans-default→mono
  // auto-resolve, and deliberately so: the mono-tuned rail letter-spacing rule
  // in index.css (data-font-family="mono") is calibrated for JetBrains Mono's
  // metrics and would harm OpenDyslexicMono's dyslexia-friendly wider glyphs.
  const { result } = renderHook(() => useZoom())
  act(() => result.current.setFontFamily('opendyslexic'))
  expect(document.documentElement.dataset.fontFamily).toBe('opendyslexic')
})

test('opendyslexic persists to localStorage under mc-font-family', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.setFontFamily('opendyslexic'))
  expect(localStorage.getItem('mc-font-family')).toBe('opendyslexic')
})

test('opendyslexic overrides --mono to OpenDyslexicMono for code blocks', () => {
  // Code blocks and inline code read var(--mono). Without this override the
  // dashboard would render body text in OpenDyslexic but keep JetBrains Mono
  // for code — inconsistent for a user who picked the font for legibility.
  const { result } = renderHook(() => useZoom())
  const readMono = () => document.documentElement.style.getPropertyValue('--mono')

  act(() => result.current.setFontFamily('opendyslexic'))
  expect(readMono()).toContain("'OpenDyslexicMono'")
  expect(readMono()).toContain('monospace')
})

test('leaving opendyslexic clears the --mono inline override', () => {
  // Once the user switches away, the :root default (JetBrains Mono) must take
  // over again. If the inline value stuck, sans/mono/system would render body
  // in their own family but keep code blocks in OpenDyslexicMono — mismatch.
  const { result } = renderHook(() => useZoom())
  const readMono = () => document.documentElement.style.getPropertyValue('--mono')

  act(() => result.current.setFontFamily('opendyslexic'))
  expect(readMono()).toContain("'OpenDyslexicMono'")

  act(() => result.current.setFontFamily('sans'))
  // The inline --mono is removed so the :root declaration takes over. Reading
  // the inline value returns the empty string (jsdom exposes only inline
  // style through .style.getPropertyValue).
  expect(readMono()).toBe('')
})

test('opendyslexic in CLI mode resolves body to the mono variant, keeps data-font-family', () => {
  // Mirrors the sans→mono auto-resolve for CLI, but preserves the user's
  // actual choice in data-font-family. The mono-only rail letter-spacing rule
  // in index.css (data-font-family="mono") is JetBrains-Mono-tuned and would
  // harm OpenDyslexicMono's dyslexia-friendly wider glyphs, so this branch
  // deliberately avoids triggering it.
  const html = document.documentElement
  const readBody = () => html.style.getPropertyValue('--font-body')

  html.dataset.ui = 'cli'
  try {
    const { result } = renderHook(() => useZoom())
    act(() => result.current.setFontFamily('opendyslexic'))

    // Body renders in OpenDyslexicMono, not the proportional face.
    expect(readBody()).toContain("'OpenDyslexicMono'")
    // But the DOM attribute reports the user's chosen family, not "mono".
    expect(html.dataset.fontFamily).toBe('opendyslexic')
  } finally {
    delete html.dataset.ui
  }
})
