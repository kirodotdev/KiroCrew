import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { createRef, type MutableRefObject } from 'react'
import { renderWithProviders } from './helpers'
import PerformanceTab from '../pages/system/PerformanceTab'
import type { PlaneState } from '../pages/SystemPage'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

// Fields the static info block guarantees; every frame below carries these.
const STATIC = {
  hostname: 'test',
  os: 'linux',
  python: '3.12',
  cwd: '/tmp',
  arch: 'x86_64',
  cpu_count: 8,
  load_1m: 1,
  load_5m: 1,
  load_15m: 1,
  proc_cpu_pct: 1,
  proc_mem_mb: 200,
  thread_count: 10,
  ip: '127.0.0.1',
  mcp_total: 2,
}

const systemMock = vi.fn()

vi.mock('../api/client', () => ({
  api: { system: () => systemMock() },
}))

async function mountAndSample() {
  const ref = createRef<PlaneState>() as MutableRefObject<PlaneState>
  ref.current = {}
  renderWithProviders(<PerformanceTab planeStateRef={ref} />)
  await waitFor(() => {
    expect(ref.current.performance?.history.length).toBeGreaterThan(0)
  }, { timeout: 8000 })
  return ref
}

describe('PerformanceTab with a partial metrics frame', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('renders placeholders for failed probes and never lets NaN into the history', async () => {
    // Failed cpu, memory-used and network probes: the server skips their keys,
    // while the cached static info still supplies mem_total_gb. An ordinary
    // outcome, not a corrupt payload.
    systemMock.mockResolvedValue({
      ...STATIC,
      mem_total_gb: 16,
      disk_total_gb: 500,
      disk_free_gb: 250,
    })
    const ref = await mountAndSample()

    // cpu_pct and mem_used_gb are absent, so the CPU and Memory rail tiles show
    // the placeholder instead of a percentage computed from nothing.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)

    // Absent probes degrade their history samples to 0; the intact disk probe
    // still yields its real value. NaN must never enter the buffer — it would
    // render as invalid clip-path vertices, a silently blank trace.
    const pt = ref.current.performance!.history[0]
    expect(pt.cpu).toBe(0)
    expect(pt.mem).toBe(0)
    expect(pt.disk).toBeCloseTo(50)
    expect(pt.netRx).toBe(0)
    expect(pt.netTx).toBe(0)
    for (const v of [pt.cpu, pt.mem, pt.disk, pt.netRx, pt.netTx]) {
      expect(Number.isFinite(v)).toBe(true)
    }
  })

  it('records a missing disk_free_gb sample as 0, never as a full disk', async () => {
    // A frame with a total but no free reading: coercing the absent operand to
    // 0 would invert "unmeasured" into a false 100%-full alarm on the graph.
    systemMock.mockResolvedValue({
      ...STATIC,
      cpu_pct: 25,
      mem_used_gb: 4,
      mem_total_gb: 16,
      net_rx_kbs: 10,
      net_tx_kbs: 5,
      disk_total_gb: 500,
    })
    const ref = await mountAndSample()
    const pt = ref.current.performance!.history[0]
    expect(pt.disk).toBe(0)
  })

  it('shows the placeholder for a zero total instead of dividing by a stand-in', async () => {
    // Declared supported by the topbar regression tests: mem_total_gb 0 with a
    // finite mem_used_gb. The rail must agree with the topbar's '—', not
    // fabricate 400% from a stand-in denominator.
    systemMock.mockResolvedValue({
      ...STATIC,
      cpu_pct: 25,
      mem_used_gb: 4,
      mem_total_gb: 0,
      disk_total_gb: 0,
      disk_free_gb: 0,
      net_rx_kbs: 0,
      net_tx_kbs: 0,
    })
    await mountAndSample()
    expect(screen.queryByText(/400/)).toBeNull()
    // Memory and Disk tiles both read '—'.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2)
  })
})
