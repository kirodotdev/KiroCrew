/**
 * While the device reports a fold the side panel docks right whatever is
 * stored (a bottom row would span both segments), and the stored choice is
 * read, never written, so it comes back when the device is flattened.
 */
import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest'
import { cleanup, renderHook } from '@testing-library/react'

const segmentsMock = vi.hoisted(() => ({ value: null as null | { x: number; width: number }[] }))
vi.mock('../hooks/useViewportSegments', async (orig) => ({
  ...(await orig<typeof import('../hooks/useViewportSegments')>()),
  useViewportSegments: () => segmentsMock.value,
}))

const KEY = 'mc-side-panel-dock'

// The dock store reads storage once, at import.
async function freshDock() {
  vi.resetModules()
  return (await import('../hooks/useSidePanelDock')).useSidePanelDock
}

describe('the side panel dock beside a fold', () => {
  beforeEach(() => { localStorage.setItem(KEY, 'bottom') })
  afterEach(() => {
    cleanup()
    segmentsMock.value = null
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('is drawn at the right and locked, and storage keeps the bottom dock', async () => {
    const useSidePanelDock = await freshDock()
    const setItem = vi.spyOn(Storage.prototype, 'setItem')
    segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
    const { result } = renderHook(() => useSidePanelDock())
    expect(result.current[0]).toBe('right')
    expect(result.current[2]).toBe(true)
    expect(setItem).not.toHaveBeenCalled()
    expect(localStorage.getItem(KEY)).toBe('bottom')
  })

  it('flattening gives the stored bottom dock back', async () => {
    const useSidePanelDock = await freshDock()
    segmentsMock.value = [{ x: 0, width: 475.5 }, { x: 475.5, width: 475.5 }]
    const { result, rerender } = renderHook(() => useSidePanelDock())
    expect(result.current[0]).toBe('right')
    segmentsMock.value = null
    rerender()
    expect(result.current[0]).toBe('bottom')
    expect(result.current[2]).toBe(false)
  })

  it('with no fold reported, the stored dock is drawn as before', async () => {
    const useSidePanelDock = await freshDock()
    const { result } = renderHook(() => useSidePanelDock())
    expect(result.current[0]).toBe('bottom')
    expect(result.current[2]).toBe(false)
  })
})
