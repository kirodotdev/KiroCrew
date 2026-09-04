import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { waitFor } from '@testing-library/react'
import { createRef, type MutableRefObject } from 'react'
import { renderWithProviders } from './helpers'
import PerformanceTab from '../pages/system/PerformanceTab'
import type { PlaneState } from '../pages/SystemPage'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const SYSTEM = {
  cpu_pct: 40, mem_used_gb: 8, mem_total_gb: 16, mem_free_gb: 8,
  disk_total_gb: 500, disk_free_gb: 250, net_rx_kbs: 10, net_tx_kbs: 5,
  hostname: 'test', os: 'linux', python: '3.12', cwd: '/tmp',
  arch: 'x86_64', cpu_count: 8, load_1m: 1, load_5m: 1, load_15m: 1,
  proc_cpu_pct: 1, proc_mem_mb: 200, thread_count: 10, ip: '127.0.0.1',
  mcp_total: 2,
}

vi.mock('../api/client', () => ({
  api: { system: () => Promise.resolve(SYSTEM) },
}))

/**
 * Mounts PerformanceTab DIRECTLY with a ref this test owns.
 *
 * Going through SystemPage could not isolate the defect: Performance and Services
 * share the `['system']` query key, so fetches accrue while the other plane is
 * mounted and any "samples <= fetches" assertion leaves the duplicate room to
 * hide — two formulations of that test passed with the bug still present. Owning
 * the ref lets the persisted guard be asserted directly instead of inferred from
 * the rendered trace.
 */
describe('PerformancePlaneState persists the sampling guard with the history', () => {
  beforeEach(() => { vi.useRealTimers() })
  afterEach(() => { vi.restoreAllMocks() })

  it('saves lastSampleAt, and a remount with that state adds no sample', async () => {
    const ref = createRef<PlaneState>() as MutableRefObject<PlaneState>
    ref.current = {}

    const first = renderWithProviders(<PerformanceTab planeStateRef={ref} />)
    await waitFor(() => {
      expect(ref.current.performance?.history.length).toBeGreaterThan(0)
    }, { timeout: 8000 })

    const savedHistory = ref.current.performance!.history.length
    const savedAt = ref.current.performance!.lastSampleAt

    // The guard must have been persisted alongside the samples. Zero here means
    // a remount cannot recognise the cached payload it already folded in.
    expect(savedAt).toBeGreaterThan(0)

    first.unmount()

    // Remount against the SAME state. react-query serves the cached payload, so
    // the restored guard is the only thing standing between that replay and a
    // duplicate sample.
    renderWithProviders(<PerformanceTab planeStateRef={ref} />)
    await waitFor(() => {
      expect(ref.current.performance?.history.length).toBeGreaterThan(0)
    }, { timeout: 8000 })

    expect(ref.current.performance!.history.length).toBe(savedHistory)
    expect(ref.current.performance!.lastSampleAt).toBe(savedAt)
  })
})
