import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SystemPage from '../pages/SystemPage'

// ResizeObserver stub for jsdom
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as typeof ResizeObserver

vi.mock('../api/client', () => ({
  api: {
    sessionsMemory: () => Promise.resolve({
      sessions: [],
      tasks: [],
      totals: { rss_mb: 0, runtimes: 0, host_mb: 16384, host_pct: 0, rss_is_upper_bound: false },
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

describe('SystemPage URL handling (finding 2)', () => {
  it('defaults to Sessions plane when no ?plane= param', async () => {
    renderWithProviders(<SystemPage />)
    await waitFor(() => {
      const tabs = screen.getAllByRole('tab')
      const sessionsTab = tabs.find(t => t.getAttribute('aria-selected') === 'true')
      expect(sessionsTab).toBeDefined()
      expect(sessionsTab!.textContent).toContain('Sessions')
    })
  })

  it('opens Performance plane from ?plane=performance', async () => {
    renderWithProviders(<SystemPage />, { route: '/?plane=performance' })
    await waitFor(() => {
      const tabs = screen.getAllByRole('tab')
      const active = tabs.find(t => t.getAttribute('aria-selected') === 'true')
      expect(active).toBeDefined()
      expect(active!.textContent).toContain('Performance')
    })
  })

  it('opens Services plane from ?plane=services', async () => {
    renderWithProviders(<SystemPage />, { route: '/?plane=services' })
    await waitFor(() => {
      const tabs = screen.getAllByRole('tab')
      const active = tabs.find(t => t.getAttribute('aria-selected') === 'true')
      expect(active).toBeDefined()
      expect(active!.textContent).toContain('Services')
    })
  })

  it('falls back to sessions for an invalid plane value', async () => {
    renderWithProviders(<SystemPage />, { route: '/?plane=invalid' })
    await waitFor(() => {
      const tabs = screen.getAllByRole('tab')
      const active = tabs.find(t => t.getAttribute('aria-selected') === 'true')
      expect(active).toBeDefined()
      expect(active!.textContent).toContain('Sessions')
    })
  })

  it('clicking a tab does not break existing ?tab=system entry point', async () => {
    // DeveloperPage passes embedded + routes to ?tab=system, our ?plane=
    // param coexists with that.
    renderWithProviders(<SystemPage />, { route: '/?tab=system' })
    await waitFor(() => {
      const tabs = screen.getAllByRole('tab')
      const sessionsTab = tabs.find(t => t.getAttribute('aria-selected') === 'true')
      expect(sessionsTab).toBeDefined()
    })
    // Click Performance tab
    const perfTab = screen.getAllByRole('tab').find(t => t.textContent?.includes('Performance'))
    expect(perfTab).toBeDefined()
    fireEvent.click(perfTab!)
    await waitFor(() => {
      const active = screen.getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')
      expect(active!.textContent).toContain('Performance')
    })
  })
})

describe('SystemPage keeps Performance history across plane flips', () => {
  // The graph needs two samples before it draws, and samples arrive on a 2s
  // refetch interval, so this one case drives the clock. Scoped to this block so
  // the rest of the file keeps real timers.
  beforeEach(() => { vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers() })

  it('does not fall back to "Collecting samples" after leaving and returning', async () => {
    // A performance graph exists to answer "what just happened?". If its samples
    // are dropped on every plane flip it can only ever show what arrived after
    // the user got there, which defeats the feature. The samples therefore live
    // in the shell's plane-state ref, not in the tab's own useState.
    renderWithProviders(<SystemPage />, { route: '/?plane=performance' })

    // Two polls => two samples => the graph draws instead of showing the
    // empty state. Without this there is no history to lose and the test
    // would pass vacuously.
    await vi.advanceTimersByTimeAsync(2200)
    await vi.advanceTimersByTimeAsync(2200)
    expect(screen.queryByText('Collecting samples…')).toBeNull()

    const goTo = async (label: string) => {
      const tab = screen.getAllByRole('tab').find(t => t.textContent?.includes(label))
      expect(tab).toBeDefined()
      fireEvent.click(tab!)
      await vi.advanceTimersByTimeAsync(50)
      const active = screen.getAllByRole('tab').find(t => t.getAttribute('aria-selected') === 'true')
      expect(active!.textContent).toContain(label)
    }

    await goTo('Sessions')
    await goTo('Performance')

    // The empty state must not come back: on the previous implementation
    // (history in the tab's own useState) it did.
    expect(screen.queryByText('Collecting samples…')).toBeNull()
  })
})
