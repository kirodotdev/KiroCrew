/**
 * MochiPage — the INTERACTIVE half of the dashboard page.
 *
 * `src/test/MochiPageCoverage.test.tsx` renders the live view and its empty
 * states; what had no test is everything that only happens once a panel has rows
 * and the user acts on them:
 *
 *  - the pager shared by Recent Activity and the plan timeline (it renders only
 *    past ten rows, so a broken slice window is invisible until then);
 *  - cancel / reopen on a watch row, which go to DIFFERENT payloads on the same
 *    endpoint — reopen is the undo for the button one column over, and the
 *    stopPropagation on both is what keeps the click from also expanding the row;
 *  - the add-watch form, whose guard (blank label / target / category) and
 *    clear-only-on-success behaviour exist because the form used to blank itself
 *    the moment mutate() was called, discarding what the user typed on a failure;
 *  - pinned files: mark-seen and unpin.
 *
 * The page's only seam is `./api`, so that module is mocked and nothing dials.
 * `importOriginal` is spread rather than replaced because `dashboardData` reads
 * `TERMINAL_STATUSES` from it and would filter against `undefined` otherwise.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { CompanionStats, MochiSettings, WatchItem } from '../api'

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    probeEnabled: vi.fn(),
    enableMochi: vi.fn(),
    getPetState: vi.fn(),
    getPlan: vi.fn(),
    getActivity: vi.fn(),
    getMochiVersion: vi.fn(),
    getStats: vi.fn(),
    getSettings: vi.fn(),
    getWatchlist: vi.fn(),
    updateWatchlist: vi.fn(),
    getPinned: vi.fn(),
    unpinFile: vi.fn(),
    markPinnedSeen: vi.fn(),
  }
})

import * as api from '../api'
import MochiPage from '../MochiPage'

function watchItem(over: Partial<WatchItem> = {}): WatchItem {
  return {
    id: 'zzq-w1',
    label: 'zzq label',
    kind: 'url',
    target: 'https://example.invalid/p',
    status: 'watching',
    priority: 'normal',
    createdAt: '2026-07-30T08:00:00Z',
    nextCheckAfter: '2026-07-30T12:00:00Z',
    checkCount: 3,
    failCount: 0,
    maxFailCount: 10,
    maxWatchDurationHours: 168,
    checkIntervalMins: 10,
    notifyOnChange: true,
    autoComplete: true,
    source: 'api',
    history: [],
    ...over,
  } as WatchItem
}

const STATS = {
  firstLaunch: '2026-07-01T00:00:00Z',
  streak: 5,
  lastActiveDate: '2026-07-30',
  companionSeconds: 7200,
  messages: { sent: 12, received: 8 },
  walkSteps: 140,
  screenshots: 3,
  peeks: 4,
  drags: 2,
  thinkingSeconds: 120,
  latestActiveTime: '02:10',
  earliestActiveTime: '06:30',
  moods: { curious: 6 },
  longestChat: 40,
  busiestDay: { date: '2026-07-28', messages: 9 },
  lastMemoryHour: 21,
  celebratedMilestones: [],
} as CompanionStats

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  })
  return render(
    <QueryClientProvider client={qc}>
      <MochiPage />
    </QueryClientProvider>,
  )
}

/** Resolve once the enable gate has been passed and the live view is up. */
async function waitForLive() {
  await waitFor(() => expect(screen.getByText('Recent Activity')).toBeTruthy())
}

beforeEach(() => {
  vi.mocked(api.probeEnabled).mockResolvedValue('enabled')
  vi.mocked(api.getPetState).mockResolvedValue({ state: 'working', mood: 'happy' })
  vi.mocked(api.getPlan).mockResolvedValue({ narrative: 'zzq narrative', tasks: [] })
  vi.mocked(api.getActivity).mockResolvedValue({ entries: [] })
  vi.mocked(api.getMochiVersion).mockResolvedValue('9.9.9')
  vi.mocked(api.getStats).mockResolvedValue(STATS)
  vi.mocked(api.getSettings).mockResolvedValue({ petName: 'Nori' } as MochiSettings)
  vi.mocked(api.getWatchlist).mockResolvedValue({ items: [] })
  vi.mocked(api.updateWatchlist).mockResolvedValue({ updated: true, items: [] })
  vi.mocked(api.getPinned).mockResolvedValue({ pins: [] })
  vi.mocked(api.unpinFile).mockResolvedValue({ ok: true } as never)
  vi.mocked(api.markPinnedSeen).mockResolvedValue({ ok: true } as never)
})

afterEach(() => {
  vi.clearAllMocks()
})

// ── The pager, shared by activity and the plan timeline ──────────────────────

describe('MochiPage pagination', () => {
  /** 12 entries: two pages at PER_PAGE = 10. */
  function activity(count: number) {
    return {
      entries: Array.from({ length: count }, (_, i) => ({
        // Descending timestamps so the shaped order matches the index.
        ts: `2026-07-30T${String(23 - i).padStart(2, '0')}:00:00Z`,
        type: 'memory',
        content: `zzq-entry-${i}`,
      })),
    }
  }

  it('hides the pager while everything fits on one page', async () => {
    vi.mocked(api.getActivity).mockResolvedValue(activity(10))
    vi.mocked(api.getPlan).mockResolvedValue({ narrative: '—', tasks: [] })
    renderPage()
    await waitForLive()
    expect(await screen.findByText('zzq-entry-0')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Next' })).toBeNull()
  })

  it('pages activity forward and back, and bounds the buttons at each end', async () => {
    vi.mocked(api.getActivity).mockResolvedValue(activity(12))
    // Keep the narrative out of the list so the row count is exactly the entries.
    vi.mocked(api.getPlan).mockResolvedValue({ narrative: '—', tasks: [] })
    renderPage()
    await waitForLive()

    expect(await screen.findByText('1–10 of 12')).toBeTruthy()
    const prev = screen.getByRole('button', { name: 'Prev' }) as HTMLButtonElement
    const next = screen.getByRole('button', { name: 'Next' }) as HTMLButtonElement
    // First page: nothing before it.
    expect(prev.disabled).toBe(true)
    expect(next.disabled).toBe(false)
    expect(screen.getByText('zzq-entry-0')).toBeTruthy()
    expect(screen.queryByText('zzq-entry-11')).toBeNull()

    fireEvent.click(next)
    expect(await screen.findByText('11–12 of 12')).toBeTruthy()
    expect(screen.getByText('zzq-entry-11')).toBeTruthy()
    expect(screen.queryByText('zzq-entry-0')).toBeNull()
    // Last page: nothing after it.
    expect((screen.getByRole('button', { name: 'Next' }) as HTMLButtonElement).disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Prev' }))
    expect(await screen.findByText('1–10 of 12')).toBeTruthy()
  })

  it('renders plan rows with their times and strikes the done ones', async () => {
    vi.mocked(api.getPlan).mockResolvedValue({
      narrative: 'zzq narrative',
      tasks: [
        { type: 'notify', action: { message: 'zzq-task-open' }, execute_after: '2026-07-30T10:30:00Z' },
        {
          type: 'notify',
          action: { message: 'zzq-task-done' },
          execute_after: '2026-07-30T11:30:00Z',
          done: true,
        },
      ],
    })
    renderPage()
    await waitForLive()
    const open = await screen.findByText('zzq-task-open')
    const done = screen.getByText('zzq-task-done')
    expect(open.closest('div')?.className).not.toContain('line-through')
    expect(done.closest('div')?.className).toContain('line-through')
    // The narrative is real prose here, so it heads the card as well.
    expect(screen.getAllByText('zzq narrative').length).toBeGreaterThan(0)
  })
})

// ── Watch rows ──────────────────────────────────────────────────────────────

describe('MochiPage watch rows', () => {
  it('expands one row at a time and shows its detail fields', async () => {
    vi.mocked(api.getWatchlist).mockResolvedValue({
      items: [
        watchItem({
          id: 'zzq-w1',
          label: 'zzq first',
          notes: 'zzq notes',
          triggerCondition: 'zzq trigger',
        } as Partial<WatchItem>),
        watchItem({ id: 'zzq-w2', label: 'zzq second' }),
      ],
    })
    renderPage()
    await waitForLive()

    const first = (await screen.findByText('zzq first')).closest('[aria-expanded]')!
    expect(first.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(first)
    expect(first.getAttribute('aria-expanded')).toBe('true')
    expect(screen.getByText('zzq notes')).toBeTruthy()
    expect(screen.getByText('zzq trigger')).toBeTruthy()
    expect(screen.getByText('3 checks')).toBeTruthy()
    expect(screen.getByText(/^Next: /)).toBeTruthy()

    // Opening the second closes the first — one detail panel at a time.
    const second = screen.getByText('zzq second').closest('[aria-expanded]')!
    fireEvent.click(second)
    expect(first.getAttribute('aria-expanded')).toBe('false')
    expect(second.getAttribute('aria-expanded')).toBe('true')

    // Clicking the open one again collapses it.
    fireEvent.click(second)
    expect(second.getAttribute('aria-expanded')).toBe('false')
  })

  it('cancels a live row without also expanding it', async () => {
    vi.mocked(api.getWatchlist).mockResolvedValue({ items: [watchItem()] })
    renderPage()
    await waitForLive()

    const row = (await screen.findByText('zzq label')).closest('[aria-expanded]')!
    fireEvent.click(screen.getByRole('button', { name: 'Stop watching' }))
    await waitFor(() =>
      expect(api.updateWatchlist).toHaveBeenCalledWith({ cancel: ['zzq-w1'] }),
    )
    // stopPropagation: acting on a row must not toggle the row it acted on.
    expect(row.getAttribute('aria-expanded')).toBe('false')
  })

  it('reopens a cancelled row through the update payload, not a second cancel', async () => {
    vi.mocked(api.getWatchlist).mockResolvedValue({
      items: [watchItem({ status: 'cancelled' })],
    })
    renderPage()
    await waitForLive()

    fireEvent.click(await screen.findByRole('button', { name: 'Reopen' }))
    await waitFor(() =>
      expect(api.updateWatchlist).toHaveBeenCalledWith({
        update: [{ id: 'zzq-w1', status: 'watching' }],
      }),
    )
  })
})

// ── Add-watch form ──────────────────────────────────────────────────────────

describe('MochiPage add-watch form', () => {
  async function fill(label: string, target: string) {
    fireEvent.change(screen.getByRole('textbox', { name: 'Name' }), {
      target: { value: label },
    })
    fireEvent.change(screen.getByRole('textbox', { name: 'What to watch' }), {
      target: { value: target },
    })
  }

  it('adds an item and clears the fields only once the server accepted', async () => {
    renderPage()
    await waitForLive()
    await fill('zzq new', 'https://example.invalid/x')
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))

    await waitFor(() =>
      expect(api.updateWatchlist).toHaveBeenCalledWith({
        add: [{ label: 'zzq new', kind: 'url', target: 'https://example.invalid/x' }],
      }),
    )
    await waitFor(() =>
      expect((screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement).value).toBe(''),
    )
  })

  it('keeps what the user typed when the add fails, and says so', async () => {
    vi.mocked(api.updateWatchlist).mockRejectedValue(new Error('zzq rejected'))
    renderPage()
    await waitForLive()
    await fill('zzq kept', 'https://example.invalid/y')
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))

    expect(await screen.findByRole('alert')).toBeTruthy()
    // The whole point: a failed add must not discard the input.
    expect((screen.getByRole('textbox', { name: 'Name' }) as HTMLInputElement).value).toBe(
      'zzq kept',
    )
  })

  it('submits nothing when the label or the target is blank', async () => {
    renderPage()
    await waitForLive()
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))
    await fill('   ', '   ')
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))
    await fill('zzq only-label', '')
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))
    expect(api.updateWatchlist).not.toHaveBeenCalled()
  })

  it('creates a category, lowercases it, and then selects it', async () => {
    renderPage()
    await waitForLive()

    fireEvent.click(screen.getByRole('combobox', { name: 'Category' }))
    const listbox = await screen.findByRole('listbox')
    fireEvent.click(within(listbox).getByText('New category…'))

    const extra = await screen.findByRole('textbox', { name: 'Category name' })
    fireEvent.change(extra, { target: { value: '  ZZQ-Deploys  ' } })
    await fill('zzq c', 'https://example.invalid/z')
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))

    await waitFor(() =>
      expect(api.updateWatchlist).toHaveBeenCalledWith({
        add: [{ label: 'zzq c', kind: 'zzq-deploys', target: 'https://example.invalid/z' }],
      }),
    )
    // The just-created category becomes the selection, so the extra field closes.
    await waitFor(() =>
      expect(screen.queryByRole('textbox', { name: 'Category name' })).toBeNull(),
    )
  })

  it('refuses to submit a blank new category', async () => {
    renderPage()
    await waitForLive()
    fireEvent.click(screen.getByRole('combobox', { name: 'Category' }))
    const listbox = await screen.findByRole('listbox')
    fireEvent.click(within(listbox).getByText('New category…'))
    await screen.findByRole('textbox', { name: 'Category name' })

    await fill('zzq d', 'https://example.invalid/d')
    fireEvent.click(screen.getByRole('button', { name: /Watch/ }))
    expect(api.updateWatchlist).not.toHaveBeenCalled()
  })
})

// ── Pinned files ────────────────────────────────────────────────────────────

describe('MochiPage pinned files', () => {
  it('flags a changed pin, marks it seen, and unpins it', async () => {
    vi.mocked(api.getPinned).mockResolvedValue({
      pins: [
        { path: '/zzq/changed.md', label: 'zzq changed', pinnedAt: 1, updatedAt: 2 },
        { path: '/zzq/quiet.md', label: 'zzq quiet', pinnedAt: 1 },
      ],
    })
    renderPage()
    await waitForLive()

    expect(await screen.findByText('zzq changed')).toBeTruthy()
    expect(screen.getByText('/zzq/quiet.md')).toBeTruthy()
    // Only the changed pin carries the badge and the mark-seen affordance.
    expect(screen.getAllByText('changed')).toHaveLength(1)
    const seen = screen.getAllByRole('button', { name: 'Mark seen' })
    expect(seen).toHaveLength(1)

    fireEvent.click(seen[0])
    // The api function IS the mutationFn here, so react-query hands it its own
    // context as a second argument — only the first one is the payload.
    await waitFor(() =>
      expect(vi.mocked(api.markPinnedSeen).mock.calls[0]?.[0]).toBe('/zzq/changed.md'),
    )

    fireEvent.click(screen.getAllByRole('button', { name: 'Unpin' })[1])
    await waitFor(() => expect(vi.mocked(api.unpinFile).mock.calls[0]?.[0]).toBe('/zzq/quiet.md'))
  })
})
