import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from '../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * The page reads `api.chatResources` for the snapshot and, for stop actions,
 * `api.stopChatSlotIfPid` (chat rows, by sampled pid) and `api.spawnCancel` (dedicated-subagent
 * rows). Mocking all three keeps every case network-free. Each test sets the
 * resolved snapshot before rendering. */
vi.mock('../api/client', () => ({
  api: {
    chatResources: vi.fn(),
    stopChatSlotIfPid: vi.fn(),
    spawnCancel: vi.fn(),
  },
}))

import { api } from '../api/client'
import SystemMonitorPage, {
  isTopConsumer,
  type MonitorEntry,
  type MonitorSnapshot,
} from './SystemMonitorPage'

const chatResources = api.chatResources as ReturnType<typeof vi.fn>
const stopChatSlot = api.stopChatSlotIfPid as ReturnType<typeof vi.fn>
const spawnCancel = api.spawnCancel as ReturnType<typeof vi.fn>

function entry(overrides: Partial<MonitorEntry> = {}): MonitorEntry {
  return {
    kind: 'chat',
    session_key: 'sess-1',
    label: 'First chat',
    agent: 'kiro',
    pid: 1000,
    proc_count: 3,
    rss_mb: 200,
    cpu_pct: 10,
    uptime_s: 65,
    slot: 'slot-1',
    subagent_id: '',
    truncated: false,
    instance: '',
    stop_pending: false,
    ...overrides,
  }
}

function snapshot(overrides: Partial<MonitorSnapshot> = {}): MonitorSnapshot {
  return {
    entries: [entry()],
    posture: 'ample',
    available_gb: 12.5,
    host_total_gb: 32,
    cpu_count: 8,
    cgroup_used_gb: null,
    cgroup_limit_gb: null,
    sampling_supported: true,
    captured_at: 1_700_000_000,
    interval_s: 2,
    ...overrides,
  }
}

async function renderPage(snap: MonitorSnapshot = snapshot()) {
  chatResources.mockResolvedValue(snap)
  const utils = renderWithProviders(<SystemMonitorPage />)
  // The first poll is async; wait for the header strip to land before asserting.
  await screen.findByTestId('monitor-header')
  return utils
}

beforeEach(() => {
  chatResources.mockReset()
  stopChatSlot.mockReset()
  spawnCancel.mockReset()
  // The shared sortable-table hook persists the chosen column per table id; a
  // click in one test must not leak into the next test's default ordering.
  localStorage.clear()
  // Default to a visible document; individual tests override.
  Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
})

afterEach(() => {
  vi.useRealTimers()
})

describe('SystemMonitorPage — rendering', () => {
  it('renders the header strip and one row per entry', async () => {
    await renderPage(
      snapshot({
        entries: [
          entry({ session_key: 'a', label: 'Alpha', rss_mb: 300, slot: 'slot-a', pid: 1 }),
          entry({ session_key: 'b', label: 'Beta', kind: 'gateway', slot: '', rss_mb: 100, pid: 2 }),
        ],
      }),
    )
    expect(screen.getByTestId('monitor-posture')).toHaveTextContent('ample')
    expect(screen.getByTestId('monitor-available')).toBeInTheDocument()
    expect(screen.getAllByTestId('monitor-row')).toHaveLength(2)
  })

  it('renders null memory/cpu/uptime as an em dash, not zero', async () => {
    await renderPage(
      snapshot({
        entries: [entry({ rss_mb: null, cpu_pct: null, uptime_s: null, slot: 'slot-1' })],
      }),
    )
    expect(screen.getByTestId('monitor-memory')).toHaveTextContent('—')
    expect(screen.getByTestId('monitor-cpu')).toHaveTextContent('—')
  })

  it('links chat rows to their slot and leaves gateway rows unlinked', async () => {
    await renderPage(
      snapshot({
        entries: [
          entry({ label: 'Alpha', slot: 'slot-a', kind: 'chat', pid: 1 }),
          entry({ label: 'Gateway', slot: '', kind: 'gateway', pid: 2 }),
        ],
      }),
    )
    const link = screen.getByRole('link', { name: 'Alpha' })
    expect(link).toHaveAttribute('href', '/chat?sid=slot-a')
    expect(screen.queryByRole('link', { name: 'Gateway' })).toBeNull()
  })

  it('shows the cgroup bar only when the snapshot carries a limit', async () => {
    const { unmount } = await renderPage(snapshot({ cgroup_used_gb: null, cgroup_limit_gb: null }))
    expect(screen.queryByTestId('monitor-cgroup')).toBeNull()
    unmount()

    chatResources.mockResolvedValue(snapshot({ cgroup_used_gb: 4, cgroup_limit_gb: 8 }))
    renderWithProviders(<SystemMonitorPage />)
    expect(await screen.findByTestId('monitor-cgroup')).toBeInTheDocument()
  })
})

describe('SystemMonitorPage — sorting', () => {
  it('defaults to memory descending', async () => {
    await renderPage(
      snapshot({
        entries: [
          entry({ label: 'Small', rss_mb: 50, cpu_pct: 5, uptime_s: 10, slot: 's1', pid: 1 }),
          entry({ label: 'Big', rss_mb: 900, cpu_pct: 1, uptime_s: 1, slot: 's2', pid: 2 }),
          entry({ label: 'Mid', rss_mb: 400, cpu_pct: 3, uptime_s: 5, slot: 's3', pid: 3 }),
        ],
      }),
    )
    const names = screen.getAllByTestId('monitor-row').map((r) => within(r).getByRole('link').textContent)
    expect(names).toEqual(['Big', 'Mid', 'Small'])
  })

  it('re-sorts by CPU descending when the CPU header is clicked', async () => {
    await renderPage(
      snapshot({
        entries: [
          entry({ label: 'LowCpu', rss_mb: 900, cpu_pct: 5, slot: 's1', pid: 1 }),
          entry({ label: 'HighCpu', rss_mb: 100, cpu_pct: 88, slot: 's2', pid: 2 }),
        ],
      }),
    )
    fireEvent.click(screen.getByRole('button', { name: /CPU/ }))
    const names = screen.getAllByTestId('monitor-row').map((r) => within(r).getByRole('link').textContent)
    expect(names).toEqual(['HighCpu', 'LowCpu'])
  })

  it('sorts null figures last', async () => {
    await renderPage(
      snapshot({
        entries: [
          entry({ label: 'Known', rss_mb: 100, slot: 's1', pid: 1 }),
          entry({ label: 'Unknown', rss_mb: null, slot: 's2', pid: 2 }),
        ],
      }),
    )
    const names = screen.getAllByTestId('monitor-row').map((r) => within(r).getByRole('link').textContent)
    expect(names).toEqual(['Known', 'Unknown'])
  })
})

describe('isTopConsumer — highlight threshold', () => {
  it('flags a tree over 25% of the cgroup limit', () => {
    const snap = snapshot({ cgroup_limit_gb: 8, cgroup_used_gb: 3, host_total_gb: 32 })
    // 2.5 GB of an 8 GB limit = 31% > 25%.
    expect(isTopConsumer(entry({ rss_mb: 2500, cpu_pct: 1 }), snap)).toBe(true)
    // 1 GB of 8 GB = 12.5% < 25%.
    expect(isTopConsumer(entry({ rss_mb: 1000, cpu_pct: 1 }), snap)).toBe(false)
  })

  it('falls back to host total when there is no cgroup', () => {
    const snap = snapshot({ cgroup_limit_gb: null, host_total_gb: 4 })
    // 1.5 GB of 4 GB = 37.5% > 25%.
    expect(isTopConsumer(entry({ rss_mb: 1500, cpu_pct: 0 }), snap)).toBe(true)
  })

  it('flags CPU over half a core regardless of memory', () => {
    const snap = snapshot({ cgroup_limit_gb: null, host_total_gb: 32 })
    expect(isTopConsumer(entry({ rss_mb: 10, cpu_pct: 51 }), snap)).toBe(true)
    expect(isTopConsumer(entry({ rss_mb: 10, cpu_pct: 49 }), snap)).toBe(false)
  })

  it('never flags on memory when no budget is known', () => {
    const snap = snapshot({ cgroup_limit_gb: null, host_total_gb: null })
    expect(isTopConsumer(entry({ rss_mb: 999_999, cpu_pct: 0 }), snap)).toBe(false)
  })

  it('applies the highlight class to the flagged row', async () => {
    await renderPage(
      snapshot({
        cgroup_limit_gb: null,
        host_total_gb: 4,
        entries: [
          entry({ label: 'Hog', rss_mb: 2000, cpu_pct: 5, slot: 's1', pid: 1 }),
          entry({ label: 'Calm', rss_mb: 50, cpu_pct: 1, slot: 's2', pid: 2 }),
        ],
      }),
    )
    const flagged = screen.getAllByTestId('monitor-row').filter(
      (r) => r.getAttribute('data-top-consumer') === 'true',
    )
    expect(flagged).toHaveLength(1)
    expect(within(flagged[0]).getByRole('link')).toHaveTextContent('Hog')
  })
})

describe('SystemMonitorPage — polling and visibility', () => {
  it('polls on the interval while visible and pauses when hidden', async () => {
    vi.useFakeTimers()
    chatResources.mockResolvedValue(snapshot())
    renderWithProviders(<SystemMonitorPage />)

    // Prime call fires on mount.
    await act(async () => {
      await Promise.resolve()
    })
    expect(chatResources).toHaveBeenCalledTimes(1)

    // A visible interval tick polls again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(chatResources).toHaveBeenCalledTimes(2)

    // Hide the document: the interval keeps firing but must NOT poll.
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'hidden' })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    const afterHide = chatResources.mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9000)
    })
    expect(chatResources).toHaveBeenCalledTimes(afterHide)
  })

  it('stops polling after unmount', async () => {
    vi.useFakeTimers()
    chatResources.mockResolvedValue(snapshot())
    const { unmount } = renderWithProviders(<SystemMonitorPage />)
    await act(async () => {
      await Promise.resolve()
    })
    const before = chatResources.mock.calls.length
    unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15000)
    })
    expect(chatResources).toHaveBeenCalledTimes(before)
  })
})

describe('SystemMonitorPage — sampling unavailable', () => {
  it('renders an explanatory notice instead of the table', async () => {
    await renderPage(snapshot({ sampling_supported: false, entries: [] }))
    expect(screen.getByTestId('monitor-unavailable')).toBeInTheDocument()
    expect(screen.queryByTestId('monitor-table')).toBeNull()
    // Host headroom is still shown above the notice.
    expect(screen.getByTestId('monitor-header')).toBeInTheDocument()
  })
})

describe('SystemMonitorPage — stop actions', () => {
  const chatEntry = entry({ kind: 'chat', label: 'Runaway', slot: 'slot-x', session_key: 'sk-x', pid: 10, instance: 'spawn-10' })
  const subagentEntry = entry({
    kind: 'subagent',
    label: 'Background task',
    slot: '',
    session_key: 'sk-parent',
    subagent_id: 'agent-9',
    pid: 20,
  })
  const gatewayEntry = entry({ kind: 'gateway', label: 'Gateway', slot: '', session_key: 'sk-gw', pid: 30 })
  const workerEntry = entry({ kind: 'worker', label: 'Review pool', slot: '', session_key: 'sk-w', pid: 40 })

  it('offers a stop button only on chat and dedicated-subagent rows', async () => {
    await renderPage(snapshot({ entries: [chatEntry, subagentEntry, gatewayEntry, workerEntry] }))
    // Two stoppable rows (chat + subagent); gateway and worker carry none.
    expect(screen.getAllByTestId('monitor-stop')).toHaveLength(2)

    // Match a row by its name-cell text (the kind column also renders words like
    // "Gateway", so search among the row elements rather than the whole doc).
    const rowFor = (name: string): HTMLElement => {
      const row = screen
        .getAllByTestId('monitor-row')
        .find((r) => within(r).queryByText(name, { selector: 'a,span' }))
      if (!row) throw new Error(`no row for ${name}`)
      return row
    }
    expect(within(rowFor('Gateway')).queryByTestId('monitor-stop')).toBeNull()
    expect(within(rowFor('Review pool')).queryByTestId('monitor-stop')).toBeNull()
  })

  it('confirms before stopping a chat, then refreshes on success', async () => {
    const user = userEvent.setup()
    stopChatSlot.mockResolvedValue({ ok: true })
    await renderPage(snapshot({ entries: [chatEntry] }))
    const before = chatResources.mock.calls.length

    await user.click(screen.getByTestId('monitor-stop'))
    // The confirmation dialog gates the request — nothing is sent yet.
    await screen.findByRole('dialog')
    expect(stopChatSlot).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Stop runtime' }))
    await waitFor(() => expect(stopChatSlot).toHaveBeenCalledWith('slot-x', 10, 'spawn-10'))
    // Success pulls a fresh snapshot without a full reload — a poll beyond the
    // prime call.
    await waitFor(() => expect(chatResources.mock.calls.length).toBeGreaterThan(before))
    expect(screen.queryByTestId('monitor-row-error')).toBeNull()
  })

  it('keeps a stopped chat row locked until the backend reports the slot idle', async () => {
    // A second stop on a slot whose soft cancel is still pending ESCALATES to a
    // hard kill and drops its queued prompts, so the row must not re-enable on
    // the response alone: the lock holds while the snapshot says stop_pending
    // and releases once the slot is idle again.
    const user = userEvent.setup()
    stopChatSlot.mockResolvedValue({ ok: true })
    const pending = snapshot({ entries: [entry({ ...chatEntry, stop_pending: true })] })
    const idle = snapshot({ entries: [chatEntry] })
    await renderPage(snapshot({ entries: [chatEntry] }))
    // The refetch after success is held back so the window between the stop
    // response and the next snapshot is observable.
    let releaseRefetch: (s: MonitorSnapshot) => void = () => {}
    chatResources.mockImplementation(() => new Promise<MonitorSnapshot>((r) => (releaseRefetch = r)))

    await user.click(screen.getByTestId('monitor-stop'))
    await user.click(await screen.findByRole('button', { name: 'Stop runtime' }))
    await waitFor(() => expect(stopChatSlot).toHaveBeenCalledWith('slot-x', 10, 'spawn-10'))
    await waitFor(() => expect(chatResources.mock.calls.length).toBeGreaterThan(1))
    // Response is back, snapshot has not landed: the row stays locked on the
    // local lock alone (the old snapshot still says stop_pending: false).
    expect(screen.getByTestId('monitor-stop')).toBeDisabled()

    releaseRefetch(pending)
    await waitFor(() => expect(screen.getByTestId('monitor-stop')).toBeDisabled())

    // The next poll shows the slot idle -- unlocked, and a press is a fresh stop.
    chatResources.mockResolvedValue(idle)
    await waitFor(() => expect(screen.getByTestId('monitor-stop')).toBeEnabled(), { timeout: 4000 })
  })

  it('disables the stop control while the server reports a pending stop', async () => {
    await renderPage(snapshot({ entries: [entry({ ...chatEntry, stop_pending: true })] }))
    expect(screen.getByTestId('monitor-stop')).toBeDisabled()
  })

  it('renders a truncated row as lower bounds with a hint', async () => {
    await renderPage(
      snapshot({
        entries: [
          entry({ ...chatEntry, rss_mb: 1500, cpu_pct: 12.5, proc_count: 4096, truncated: true }),
        ],
      }),
    )
    expect(screen.getByTestId('monitor-memory').textContent).toMatch(/^≥/)
    expect(screen.getByTestId('monitor-cpu').textContent).toMatch(/^≥/)
    const procs = screen.getByTestId('monitor-procs')
    expect(procs.textContent).toMatch(/^≥/)
    expect(procs).toHaveAttribute('title')
  })

  it('cancels a dedicated subagent by its agent id on confirm', async () => {
    const user = userEvent.setup()
    spawnCancel.mockResolvedValue({ ok: true })
    await renderPage(snapshot({ entries: [subagentEntry] }))

    await user.click(screen.getByTestId('monitor-stop'))
    await user.click(await screen.findByRole('button', { name: 'Stop runtime' }))
    await waitFor(() => expect(spawnCancel).toHaveBeenCalledWith('agent-9'))
  })

  it('does not send the request when the confirmation is dismissed', async () => {
    const user = userEvent.setup()
    await renderPage(snapshot({ entries: [chatEntry] }))
    await user.click(screen.getByTestId('monitor-stop'))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(stopChatSlot).not.toHaveBeenCalled()
  })

  it('surfaces a stop failure inline and leaves the row in place', async () => {
    const user = userEvent.setup()
    stopChatSlot.mockRejectedValue(new Error('slot busy'))
    await renderPage(snapshot({ entries: [chatEntry] }))

    await user.click(screen.getByTestId('monitor-stop'))
    await user.click(await screen.findByRole('button', { name: 'Stop runtime' }))

    const rowError = await screen.findByTestId('monitor-row-error')
    expect(rowError).toHaveTextContent('slot busy')
    // The row is still shown — a failed stop never removes the entry.
    expect(screen.getAllByTestId('monitor-row')).toHaveLength(1)
  })

  it('treats an HTTP-200 refusal ({ok:false}) as a failed stop', async () => {
    // A remote-bound chat whose crew is unreachable answers 200 with
    // {ok:false, error, code}; that must reach the row's ErrorNotice and must
    // NOT trigger the post-success refetch.
    const user = userEvent.setup()
    stopChatSlot.mockResolvedValue({
      ok: false,
      error: 'could not reach the crew running this session to stop it',
      code: 'remote_stop_unreachable',
    })
    await renderPage(snapshot({ entries: [chatEntry] }))
    const before = chatResources.mock.calls.length

    await user.click(screen.getByTestId('monitor-stop'))
    await user.click(await screen.findByRole('button', { name: 'Stop runtime' }))

    const rowError = await screen.findByTestId('monitor-row-error')
    expect(rowError).toHaveTextContent('could not reach the crew')
    expect(screen.getAllByTestId('monitor-row')).toHaveLength(1)
    expect(chatResources.mock.calls.length).toBe(before)
  })

  it('a 409 stale_row refusal shows inline, keeps the row, and refetches', async () => {
    // The row is a sample; the backend refuses when the slot has since moved on
    // to a different runtime (the confirm dialog can sit open past a reset).
    // The refusal reaches the row's ErrorNotice AND triggers a refetch so the
    // operator sees what the slot is running now before deciding again.
    const user = userEvent.setup()
    stopChatSlot.mockRejectedValue(
      Object.assign(new Error('the sampled conversation is no longer what this slot is running'), {
        status: 409,
      }),
    )
    await renderPage(snapshot({ entries: [chatEntry] }))
    const before = chatResources.mock.calls.length

    await user.click(screen.getByTestId('monitor-stop'))
    await user.click(await screen.findByRole('button', { name: 'Stop runtime' }))

    const rowError = await screen.findByTestId('monitor-row-error')
    expect(rowError).toHaveTextContent('no longer what this slot is running')
    expect(screen.getAllByTestId('monitor-row')).toHaveLength(1)
    await waitFor(() => expect(chatResources.mock.calls.length).toBeGreaterThan(before))
    // Unlocked: nothing was stopped, so a fresh press is allowed.
    expect(screen.getByTestId('monitor-stop')).not.toBeDisabled()
  })
})
