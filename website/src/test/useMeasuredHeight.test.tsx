import { StrictMode } from 'react'
import { act, render } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { useMeasuredHeight } from '../hooks/useMeasuredHeight'

function Probe() {
  const [ref, height] = useMeasuredHeight<HTMLDivElement>()
  return <div ref={ref} data-testid="box" data-height={height} />
}

/**
 * StrictMode runs every effect's cleanup and then the effect again on mount,
 * while a callback ref runs once. A hook whose only disconnect lived in the
 * cleanup came out of that dance with a dead observer and a height frozen at
 * its mount value; the effect must re-attach to the node it still holds.
 */
describe('useMeasuredHeight', () => {
  it('keeps observing after StrictMode replays the mount effect', () => {
    const heights = [44, 211]
    let call = 0
    const rect = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(() => ({
      height: heights[Math.min(call++, heights.length - 1)], width: 280, x: 0, y: 0, top: 0, left: 0, right: 280, bottom: 0, toJSON: () => ({}),
    }) as DOMRect)
    const live: Array<{ cb: (entries: unknown[]) => void; disconnected: boolean }> = []
    const RO = vi.fn(function (this: { observe: () => void; disconnect: () => void }, cb: (entries: unknown[]) => void) {
      const rec = { cb, disconnected: false }
      live.push(rec)
      this.observe = vi.fn()
      this.disconnect = () => { rec.disconnected = true }
    })
    vi.stubGlobal('ResizeObserver', RO)
    try {
      const { getByTestId } = render(<StrictMode><Probe /></StrictMode>)
      const box = getByTestId('box')
      expect(box.getAttribute('data-height')).toBe('44')
      const alive = live.filter(r => !r.disconnected)
      expect(alive, 'exactly one observer survives the StrictMode replay').toHaveLength(1)
      act(() => { alive[0].cb([{ target: box }]) })
      expect(box.getAttribute('data-height')).toBe('211')
    } finally {
      rect.mockRestore()
      vi.unstubAllGlobals()
    }
  })
})
