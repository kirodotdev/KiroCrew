import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SystemPage from '../pages/SystemPage'

// ResizeObserver stub for jsdom
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

const SESSION_NAME = 'a session whose name is long enough to be clipped'

vi.mock('../api/client', () => ({
  api: {
    sessionsMemory: () => Promise.resolve({
      sessions: [{
        key: 'chat-1', title: SESSION_NAME, slot_key: 'chat-1', untitled: false,
        agent: 'kirocrew', channel: 'dashboard', pid: 4242, owns_runtime: true,
        prompts: 3, rss_mb: 512, procs: 2, mcp: 7, cpu_cores: 1.5,
        uptime_s: 900, credits: 12, turns: 8,
      }],
      tasks: [{
        id: 't1', task: 'child task', agent: 'kirocrew', parent: 'chat-1',
        sampled: true, rss_mb: 64, peak_rss_mb: 96, cpu_cores: 0.25,
        started_at: null, pid: 4243, shared: false,
      }],
      totals: { rss_mb: 576, runtimes: 1, host_mb: 16384, host_pct: 3.5, rss_is_upper_bound: false },
      unattributed: null,
      history: [],
    }),
    system: () => Promise.resolve({
      cpu_pct: 10, mem_used_gb: 4, mem_total_gb: 16, mem_free_gb: 12,
      disk_total_gb: 500, disk_free_gb: 300, net_rx_kbs: 0, net_tx_kbs: 0,
      hostname: 'test', os: 'linux', python: '3.12', cwd: '/tmp',
      arch: 'x86_64', cpu_count: 8, load_1m: 1, load_5m: 1, load_15m: 1,
      proc_cpu_pct: 1, proc_mem_mb: 200, thread_count: 10, ip: '127.0.0.1',
      mcp_total: 2,
    }),
  },
}))

/**
 * Regression cover for the clipped session name.
 *
 * The table is `table-layout: fixed`. A fixed table with no declared column
 * widths splits the width EQUALLY, which gave the name column ~1/11th of the
 * table and clipped every session name out of view. The fix is a real column
 * width model: `size` on each columnDef, emitted as a <colgroup>.
 *
 * These assertions fail if either half is removed — drop the <colgroup> and
 * there are no <col> elements; drop the name column's `size` and every column
 * falls back to the same default width.
 */
describe('Sessions table column width model', () => {
  beforeEach(() => { vi.useRealTimers() })
  afterEach(() => { vi.restoreAllMocks() })

  it('declares a <col> per visible column, with the name column widest', async () => {
    const { container } = renderWithProviders(<SystemPage />)

    await waitFor(() => {
      expect(screen.getByText(SESSION_NAME)).toBeTruthy()
    })

    const cols = container.querySelectorAll('table > colgroup > col')
    const headers = container.querySelectorAll('table > thead th')
    expect(cols.length).toBeGreaterThan(0)
    // One <col> per rendered header, or the widths line up against the wrong
    // columns and every value shifts one cell left.
    expect(cols.length).toBe(headers.length)

    const widths = Array.from(cols).map(c => parseFloat((c as HTMLElement).style.width))
    expect(widths.every(w => Number.isFinite(w) && w > 0)).toBe(true)

    // The name column is first and has to be wide enough to actually SHOW a
    // name. "Wider than the numeric columns" is too weak an assertion to be
    // worth making: TanStack's default width (150) already clears that bar,
    // so a name column that lost its declared `size` would still pass. Both
    // bounds below fail on that default, which is the regression that matters.
    const [nameWidth, ...rest] = widths
    expect(nameWidth).toBeGreaterThanOrEqual(200)
    expect(nameWidth).toBeGreaterThanOrEqual(2 * Math.max(...rest))
  })
})
