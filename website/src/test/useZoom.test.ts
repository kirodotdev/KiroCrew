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

test('cycleFamily rotates sans → mono → system → sans', () => {
  const { result } = renderHook(() => useZoom())
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('mono')
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('system')
  act(() => result.current.cycleFamily())
  expect(result.current.family).toBe('sans')
})
