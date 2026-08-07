import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SystemPage from '../pages/SystemPage'

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

vi.mock('../api/client', () => ({
  api: {
    sessionsMemory: () => Promise.resolve({
      sessions: [
        { key: 'chat-1', title: 'alpha session', slot_key: 'chat-1', untitled: false,
          agent: 'kirocrew', channel: 'slack', pid: 1, owns_runtime: true, prompts: 1,
          rss_mb: 100, procs: 1, mcp: 1, cpu_cores: 0.1, uptime_s: 60, credits: 1, turns: 1 },
        { key: 'chat-2', title: 'beta session', slot_key: 'chat-2', untitled: false,
          agent: 'kirocrew', channel: 'slack', pid: 2, owns_runtime: true, prompts: 1,
          rss_mb: 200, procs: 1, mcp: 1, cpu_cores: 0.2, uptime_s: 60, credits: 2, turns: 2 },
        { key: 'chat-3', title: 'gamma session', slot_key: 'chat-3', untitled: false,
          agent: 'kirocrew', channel: 'cron', pid: 3, owns_runtime: true, prompts: 1,
          rss_mb: 300, procs: 1, mcp: 1, cpu_cores: 0.3, uptime_s: 60, credits: 3, turns: 3 },
      ],
      tasks: [],
      totals: { rss_mb: 600, runtimes: 3, host_mb: 16384, host_pct: 3.6, rss_is_upper_bound: false },
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
 * `channel` is hidden on first paint, so when you fold on it the grouping value
 * has nowhere to render except the name column. Before the fix the name cell
 * fell through to the first member's own title, so a group of two Slack
 * sessions was labelled "alpha session" — a row lying about what it represents,
 * which is the exact class of defect this page was restructured to remove.
 */
describe('Sessions grouping labels the group, not its first member', () => {
  beforeEach(() => { vi.useRealTimers() })
  afterEach(() => { vi.restoreAllMocks() })

  it('shows the channel value on group rows while the channel column is hidden', async () => {
    renderWithProviders(<SystemPage />)
    await waitFor(() => expect(screen.getByText('alpha session')).toBeTruthy())

    fireEvent.click(screen.getByRole('button', { name: /channel/i }))

    await waitFor(() => {
      // Two folds: slack (2 members) and cron (1).
      expect(screen.getByText('slack')).toBeTruthy()
      expect(screen.getByText('cron')).toBeTruthy()
    })

    // Precise check: the GROUP row's own first cell must be the grouping value.
    // Asserting the members are absent would be wrong -- folds render expanded,
    // so "alpha session" legitimately appears as a member row below its group.
    const slackCell = screen.getByText('slack')
    const groupRow = slackCell.closest('tr')!
    const firstCellText = groupRow.querySelector('td')!.textContent ?? ''
    expect(firstCellText).toContain('slack')
    expect(firstCellText).not.toContain('alpha session')
  })
})
