/** WarmSwap with a Pierre chunk that outlives the swap deadline.
 *
 *  The staged impl suspends to `null` while WarmSwap still shows its own
 *  fallback (no duplicated text, no false paint). Once the deadline fail-safe
 *  reveals the surface, the inner Suspense must supply a real fallback — a
 *  slow network chunk shows readable plain text, never a blank block.
 */
import { act, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PierreCode, PierreFilePair, PierrePatch } from '../pierre'

// A chunk load slower than the deadline: every impl suspends forever, which is
// exactly what React sees while a lazy chunk is still in flight. Modelled with
// suspending components rather than a never-resolving mock factory so the
// behavior does not depend on how the module runner treats a pending factory.
vi.mock('../pierre/PierreImpl', () => {
  const pending = new Promise<never>(() => {})
  const StillLoading = () => { throw pending }
  return { PierreCodeImpl: StillLoading, PierrePatchImpl: StillLoading, PierreFilePairImpl: StillLoading }
})

const PATCH = 'diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n'

describe('WarmSwap with a chunk slower than the deadline', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  async function revealByDeadline(ui: React.ReactElement) {
    class InertRO {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    vi.stubGlobal('ResizeObserver', InertRO)
    vi.useFakeTimers()
    const spy = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(0)
    const view = render(ui)
    // Staging: exactly one visible copy of the text, none inside the hidden box.
    expect(view.container.querySelectorAll('pre')).toHaveLength(1)
    expect(view.container.querySelectorAll('[aria-hidden="true"] pre')).toHaveLength(0)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2600)
    })
    spy.mockRestore()
    return view
  }

  it('keeps plain code text visible after the deadline reveals an unresolved chunk', async () => {
    const view = await revealByDeadline(<PierreCode file={{ name: 'a.ts', contents: 'const visible = 1\n' }} />)
    expect(view.container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(0)
    expect(screen.getByText('const visible = 1')).toBeVisible()
  })

  it('keeps plain patch text visible after the deadline reveals an unresolved chunk', async () => {
    const view = await revealByDeadline(<PierrePatch patch={PATCH} />)
    expect(view.container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(0)
    expect(view.container.textContent).toContain('+b')
  })

  it('keeps both file-pair sides visible after the deadline reveals an unresolved chunk', async () => {
    const view = await revealByDeadline(
      <PierreFilePair oldFile={{ name: 'a.ts', contents: 'OLD_SIDE\n' }} newFile={{ name: 'a.ts', contents: 'NEW_SIDE\n' }} />,
    )
    expect(view.container.querySelectorAll('[aria-hidden="true"]')).toHaveLength(0)
    expect(view.container.textContent).toContain('OLD_SIDE')
    expect(view.container.textContent).toContain('NEW_SIDE')
  })
})
