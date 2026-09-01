// @vitest-environment happy-dom
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'
import { act } from 'react'

// The highlight worker pool has no failure path of its own: @pierre/diffs logs
// a worker `error` and returns, so the request in flight is never rejected and
// the surface waiting on it paints nothing at all — a code block reduced to its
// language header with an empty body. This store is the detection we add at the
// one place we hold the Worker object, and `disableWorkerPool` is what every
// mounted surface switches to once it fires.

beforeEach(() => {
  // Module-level state: each test needs its own copy of the store.
  vi.resetModules()
})

describe('pierre worker pool health', () => {
  it('starts healthy', async () => {
    const { isWorkerPoolBroken } = await import('../pierre/workerHealth')
    expect(isWorkerPoolBroken()).toBe(false)
  })

  it('latches broken and warns exactly once across repeated reports', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { isWorkerPoolBroken, markWorkerPoolBroken } = await import('../pierre/workerHealth')
    markWorkerPoolBroken('boom')
    markWorkerPoolBroken('boom again from a sibling worker')
    expect(isWorkerPoolBroken()).toBe(true)
    // One root cause can kill every worker in the pool; only the first report
    // is news.
    expect(warn).toHaveBeenCalledTimes(1)
    warn.mockRestore()
  })

  it('re-renders a subscribed surface when the pool breaks', async () => {
    const { markWorkerPoolBroken, useWorkerPoolBroken } = await import('../pierre/workerHealth')
    const seen: boolean[] = []
    function Probe() {
      const broken = useWorkerPoolBroken()
      seen.push(broken)
      return <span data-testid="probe">{String(broken)}</span>
    }
    const { getByTestId } = render(<Probe />)
    expect(getByTestId('probe').textContent).toBe('false')
    act(() => { markWorkerPoolBroken('worker died') })
    expect(getByTestId('probe').textContent).toBe('true')
    expect(seen).toContain(true)
  })

  it('drops its listener on unmount so a later report does not touch a dead tree', async () => {
    const { markWorkerPoolBroken, useWorkerPoolBroken } = await import('../pierre/workerHealth')
    function Probe() {
      useWorkerPoolBroken()
      return null
    }
    const { unmount } = render(<Probe />)
    unmount()
    // Would throw on an update to an unmounted component if the listener leaked.
    expect(() => act(() => { markWorkerPoolBroken('after unmount') })).not.toThrow()
  })
})
