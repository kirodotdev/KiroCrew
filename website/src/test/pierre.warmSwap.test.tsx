/** WarmSwap: the fallback-holding wrapper every Pierre surface renders through.
 *
 *  Coverage targets the branches the virtualizer work added: the farm-render
 *  short-circuit (fallback IS the measured geometry), the no-ResizeObserver
 *  immediate swap, the RO-driven swap once the impl paints real height, the
 *  deadline fail-safe, and the known-height freeze box on remount.
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PierreFarmHoldContext } from '../components/pierreStaging'
import { PierreFilePair, PierrePatch } from '../pierre'

// The lazy impl chunk: resolve instantly to a visible stub so tests exercise
// WarmSwap itself, not the real highlight worker.
vi.mock('../pierre/PierreImpl', () => ({
  PierreCodeImpl: () => <div data-testid="impl">impl</div>,
  PierrePatchImpl: () => <div data-testid="impl">impl</div>,
  PierreFilePairImpl: () => <div data-testid="impl">impl</div>,
}))

const PATCH = 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'

describe('WarmSwap', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('farm render short-circuits to the fallback alone', () => {
    render(
      <PierreFarmHoldContext.Provider value={true}>
        <PierrePatch patch={PATCH} />
      </PierreFarmHoldContext.Provider>,
    )
    expect(screen.queryByTestId('impl')).toBeNull()
  })

  it('uses a caller-provided FilePair fallback during farm measurement', () => {
    render(
      <PierreFarmHoldContext.Provider value={true}>
        <PierreFilePair
          oldFile={{ name: 'a.ts', contents: 'before' }}
          newFile={{ name: 'a.ts', contents: 'after' }}
          fallbackText="bounded"
          fallbackClassName="max-h-[376px] overflow-auto"
        />
      </PierreFarmHoldContext.Provider>,
    )
    expect(screen.getByText('bounded')).toHaveClass('max-h-[376px]', 'overflow-auto')
    expect(screen.queryByTestId('impl')).toBeNull()
  })

  it('notifies FilePair when WarmSwap reveals the implementation', async () => {
    let roCallback: (() => void) | null = null
    class StubRO {
      constructor(cb: () => void) { roCallback = cb }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', StubRO)
    let height = 0
    const spy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockImplementation(() => height)
    const onVisible = vi.fn()
    try {
      render(
        <PierreFilePair
          oldFile={{ name: 'a.ts', contents: 'before' }}
          newFile={{ name: 'a.ts', contents: 'after' }}
          fallbackText="bounded"
          fallbackClassName="max-h-[376px] overflow-auto"
          onVisible={onVisible}
        />,
      )
      expect(await screen.findByTestId('impl')).toBeTruthy()
      expect(onVisible).not.toHaveBeenCalled()
      height = 120
      await act(async () => { roCallback?.() })
      expect(onVisible).toHaveBeenCalledTimes(1)
    } finally {
      spy.mockRestore()
    }
  })

  it('swaps immediately when ResizeObserver is unavailable (jsdom default)', async () => {
    const orig = globalThis.ResizeObserver
    // @ts-expect-error deliberately removing the API for this branch
    delete globalThis.ResizeObserver
    try {
      render(<PierrePatch patch={PATCH} />)
      expect(await screen.findByTestId('impl')).toBeTruthy()
    } finally {
      if (orig) globalThis.ResizeObserver = orig
    }
  })

  it('holds the fallback until the impl paints, then swaps via RO', async () => {
    let roCallback: (() => void) | null = null
    class StubRO {
      constructor(cb: () => void) {
        roCallback = cb
      }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', StubRO)
    let height = 0
    const spy = vi
      .spyOn(HTMLElement.prototype, 'scrollHeight', 'get')
      .mockImplementation(function () {
        return height
      })
    try {
      render(<PierrePatch patch={PATCH} />)
      // Impl chunk resolves; box height is still 0 -> fallback stays.
      expect(await screen.findByTestId('impl')).toBeTruthy()
      const box = screen.getByTestId('impl').parentElement as HTMLElement
      expect(box.className).toContain('invisible')
      // The impl paints: RO fires with real height -> swap releases the box.
      height = 120
      await act(async () => {
        roCallback?.()
      })
      expect(box.className).not.toContain('invisible')
    } finally {
      spy.mockRestore()
    }
  })

  it('deadline fail-safe swaps even when height never crosses the threshold', async () => {
    class InertRO {
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', InertRO)
    vi.useFakeTimers()
    const spy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(0)
    try {
      render(<PierrePatch patch={PATCH} />)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2600)
      })
      const impl = screen.getByTestId('impl')
      expect((impl.parentElement as HTMLElement).className).not.toContain('invisible')
    } finally {
      spy.mockRestore()
    }
  })

  it('remount of a known surface freezes the box at the last painted height', async () => {
    class StubRO {
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', StubRO)
    const spy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(140)
    try {
      // First mount paints at 140 and records it under the content key.
      const first = render(<PierrePatch patch={PATCH} />)
      expect(await screen.findByTestId('impl')).toBeTruthy()
      first.unmount()
      // Remount: warming box must be frozen at the recorded height.
      spy.mockReturnValue(0)
      render(<PierrePatch patch={PATCH} />)
      const impl = await screen.findByTestId('impl')
      const outer = (impl.parentElement as HTMLElement).parentElement as HTMLElement
      expect(outer.style.height).toBe('140px')
      expect(outer.style.overflow).toBe('hidden')
    } finally {
      spy.mockRestore()
    }
  })
})
