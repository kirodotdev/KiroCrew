/**
 * The pinned metrics popover (shell/topbar/metricsReadout.tsx) anchors to a
 * snapshot of its trigger's box. A desktop ladder level change re-lays out the
 * header without a window resize, so it can move the trigger or hide it (the
 * capsule rung hides everything after the connection dot); the popover must
 * close rather than stay anchored to a box that is gone.
 */
import { act, renderHook } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('../api/client', () => ({
  api: {
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
  },
}))

import { useMetricsReadout } from '../shell/topbar/metricsReadout'

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('useMetricsReadout: the pinned popover and the desktop ladder', () => {
  afterEach(() => { document.body.replaceChildren() })

  it('closes when the ladder changes level, which can move or hide its trigger', () => {
    const { result, rerender } = renderHook(
      ({ level }: { level: number }) => useMetricsReadout(false, false, level),
      { wrapper, initialProps: { level: 1 } },
    )
    const trigger = document.createElement('button')
    document.body.append(trigger)
    result.current.metricsBtnRef.current = trigger
    act(() => { result.current.toggleMetricsPopover() })
    expect(result.current.metricsPopoverOpen).toBe(true)

    rerender({ level: 7 })

    expect(result.current.metricsPopoverOpen).toBe(false)
  })

  it('stays open while the level holds', () => {
    const { result, rerender } = renderHook(
      ({ level }: { level: number }) => useMetricsReadout(false, false, level),
      { wrapper, initialProps: { level: 2 } },
    )
    const trigger = document.createElement('button')
    document.body.append(trigger)
    result.current.metricsBtnRef.current = trigger
    act(() => { result.current.toggleMetricsPopover() })

    rerender({ level: 2 })

    expect(result.current.metricsPopoverOpen).toBe(true)
  })
})
