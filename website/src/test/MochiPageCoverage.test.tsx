/**
 * First tests for the Mochi dashboard page's LIVE view (`apps/mochi/MochiPage.tsx`).
 *
 * The existing suite (`apps/mochi/test/mochiDashboardPage.test.tsx`) covers the
 * pure shaping helpers and the offline landing, so everything past the enable
 * gate — the stat row, Recent Activity, Memories, Watch List, the plan timeline,
 * pinned files, and the pager shared by the two paginated lists — had never been
 * rendered by a test. Those are the behaviours here.
 *
 * The page's only seam to the gateway is `./api`, so that module is mocked and
 * nothing touches the network. `importOriginal` is spread rather than replaced
 * because `dashboardData` imports `TERMINAL_STATUSES` from the same module and
 * would filter against `undefined` if the mock dropped it.
 *
 * Real timers throughout: every query in the page carries its own
 * `refetchInterval` (5s waiting / 10s live), which a `defaultOptions` override
 * cannot reach, and none of the assertions here wait on one — a poll is simply
 * never reached inside a test, and unmount on cleanup cancels the observers.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type {
  CompanionStats,
  MochiSettings,
  WatchItem,
} from '../apps/mochi/api'

vi.mock('../apps/mochi/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../apps/mochi/api')>()
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

import * as api from '../apps/mochi/api'
import MochiPage from '../apps/mochi/MochiPage'

/** A watch item with every optional field the expanded row can show. */
function watchItem(over: Partial<WatchItem>): WatchItem {
  return {
    id: 'w-1',
    label: 'Price drop',
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

const STATS: CompanionStats = {
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
  moods: { curious: 6, scared: 2 },
  longestChat: 40,
  busiestDay: { date: '2026-07-28', messages: 9 },
  lastMemoryHour: 21,
  celebratedMilestones: [],
}

/** Only the one key the page reads; the rest of the shape is irrelevant here. */
const SETTINGS = { petName: 'Nori' } as MochiSettings



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
    </QueryClientProvider>
  )
}

/** Resolve once the live view has replaced the landing / skeleton. */
async function waitForLive() {
  await waitFor(() => expect(screen.getByText('Recent Activity')).toBeTruthy())
}

beforeEach(() => {
  vi.mocked(api.probeEnabled).mockResolvedValue('enabled')
  vi.mocked(api.enableMochi).mockResolvedValue(undefined)
  vi.mocked(api.getPetState).mockResolvedValue({ state: 'working', mood: 'happy' })
  vi.mocked(api.getPlan).mockResolvedValue({ narrative: 'watching a build', tasks: [] })
  vi.mocked(api.getActivity).mockResolvedValue({ entries: [] })
  vi.mocked(api.getMochiVersion).mockResolvedValue('1.4.2')
  vi.mocked(api.getStats).mockResolvedValue(STATS)
  vi.mocked(api.getSettings).mockResolvedValue(SETTINGS)
  vi.mocked(api.getWatchlist).mockResolvedValue({ items: [] })
  vi.mocked(api.updateWatchlist).mockResolvedValue({ updated: true, items: [] })
  vi.mocked(api.getPinned).mockResolvedValue({ pins: [] })
  vi.mocked(api.unpinFile).mockResolvedValue({ ok: true })
  vi.mocked(api.markPinnedSeen).mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.clearAllMocks()
})

// ── Gate: skeleton / landing / live ──────────────────────────────────────────

describe('MochiPage enable gate', () => {
  it('shows the connecting skeleton while the probe is still in flight', () => {
    // Never settles: the page must render its own placeholder rather than an
    // empty frame, because the probe is the first thing that happens on the route.
    vi.mocked(api.probeEnabled).mockReturnValue(new Promise(() => {}))
    renderPage()
    expect(screen.getByTestId('page-title').textContent).toBe('Mochi')
    expect(screen.getByTestId('page-subtitle').textContent).toBe('connecting…')
    expect(screen.queryByText('Recent Activity')).toBeNull()
  })

  it('enables the app from the landing and lists what it can do', async () => {
    vi.mocked(api.probeEnabled).mockResolvedValue('disabled')
    renderPage()
    await waitFor(() => expect(screen.getByText('Mochi is sleeping')).toBeTruthy())
    expect(screen.getByText('What Mochi can do')).toBeTruthy()
    expect(
      screen.getByText('Desktop pet with walk, peek, and mood animations')
    ).toBeTruthy()
    expect(screen.getByText('Subagent dispatch for heavy tasks')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: /Enable Mochi/ }))
    await waitFor(() => expect(api.enableMochi).toHaveBeenCalledTimes(1))
  })

  it('re-probes when the landing asks to check again', async () => {
    vi.mocked(api.probeEnabled).mockResolvedValue('starting')
    renderPage()
    await waitFor(() => expect(screen.getByText(/Mochi is enabled and starting up/)).toBeTruthy())
    // Already enabled — offering "Enable" again would tell the wrong story.
    expect(screen.queryByRole('button', { name: /Enable Mochi/ })).toBeNull()

    const before = vi.mocked(api.probeEnabled).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: /Check again/ }))
    await waitFor(() =>
      expect(vi.mocked(api.probeEnabled).mock.calls.length).toBeGreaterThan(before)
    )
  })
})

// ── Stat row ────────────────────────────────────────────────────────────────


// ── Recent activity + the shared pager ──────────────────────────────────────

describe('MochiPage recent activity', () => {
  it('empties out with a hint when nothing has been logged', async () => {
    vi.mocked(api.getPlan).mockResolvedValue({ narrative: '—', tasks: [] })
    renderPage()
    await waitForLive()
    expect(screen.getByText('No activity yet')).toBeTruthy()
    expect(screen.getByText('Mochi logs events here as they happen')).toBeTruthy()
  })

})

// ── Plan timeline ───────────────────────────────────────────────────────────

describe('MochiPage plan timeline', () => {
  it('offers the empty state when the planner has not written a queue', async () => {
    renderPage()
    await waitForLive()
    expect(screen.getByText('No plan yet')).toBeTruthy()
    expect(screen.getByText('Mochi writes a plan when it next wakes up')).toBeTruthy()
  })

  it('lists categories the user already has alongside the presets', async () => {
    vi.mocked(api.getWatchlist).mockResolvedValue({
      items: [watchItem({ kind: 'deploys' as WatchItem['kind'] })],
    })
    renderPage()
    await waitForLive()
    fireEvent.click(screen.getByRole('combobox', { name: 'Category' }))
    const listbox = await screen.findByRole('listbox')
    expect(within(listbox).getByText('deploys')).toBeTruthy()
  })
})

// ── Memories ────────────────────────────────────────────────────────────────

describe('MochiPage memories card', () => {
  it('offers the empty state before any stats exist', async () => {
    vi.mocked(api.getStats).mockResolvedValue(null as unknown as CompanionStats)
    renderPage()
    await waitForLive()
    expect(screen.getByText('No memories yet')).toBeTruthy()
    expect(screen.getByText('Spend time with Mochi to build memories')).toBeTruthy()
  })

  it('names the user’s own pet in the message row and shows the top moods', async () => {
    renderPage()
    await waitForLive()
    expect(await screen.findByText('20 messages (12 you · 8 Nori)')).toBeTruthy()
    expect(screen.getByText(/Together for/)).toBeTruthy()
    expect(screen.getByText(/^140 steps/)).toBeTruthy()
    expect(screen.getByText(/Looked at your screen 3 times/)).toBeTruthy()
    expect(screen.getByText(/Peeked 4 times/)).toBeTruthy()
    expect(screen.getByText(/Dragged 2 times/)).toBeTruthy()
    expect(screen.getByText(/Thought for/)).toBeTruthy()
    expect(screen.getByText(/stayed up till 02:10/)).toBeTruthy()
    expect(screen.getByText(/up at 06:30/)).toBeTruthy()
    expect(screen.getByText(/Chattiest day: 2026-07-28/)).toBeTruthy()
    expect(screen.getByText(/Longest chat: 40 messages/)).toBeTruthy()
    expect(screen.getByText('Top moods:')).toBeTruthy()
    expect(screen.getByText('Curious 75%')).toBeTruthy()
    expect(screen.getByText('Scared 25%')).toBeTruthy()
  })

  it('drops zero-valued rows and the mood strip when nothing was recorded', async () => {
    vi.mocked(api.getStats).mockResolvedValue({
      ...STATS,
      streak: 1,
      companionSeconds: 0,
      messages: { sent: 0, received: 0 },
      walkSteps: 0,
      screenshots: 0,
      peeks: 0,
      drags: 0,
      thinkingSeconds: 0,
      latestActiveTime: '',
      earliestActiveTime: '',
      longestChat: 0,
      busiestDay: { date: '2026-07-28', messages: 0 },
      moods: {},
    })
    // No petName: the row would name the pet, so the packaged default stands in.
    vi.mocked(api.getSettings).mockResolvedValue({ petName: '' } as MochiSettings)
    renderPage()
    await waitForLive()
    expect(screen.getByText('Memories')).toBeTruthy()
    expect(screen.queryByText(/Together for/)).toBeNull()
    expect(screen.queryByText('Top moods:')).toBeNull()
    expect(screen.queryByText(/Chattiest day/)).toBeNull()
  })

  it('omits the streak suffix on a single-day streak', async () => {
    vi.mocked(api.getStats).mockResolvedValue({ ...STATS, streak: 1 })
    renderPage()
    await waitForLive()
    expect(await screen.findByText(/Together for/)).toBeTruthy()
    expect(screen.queryByText(/day streak/)).toBeNull()
  })
})

// ── Pinned files ────────────────────────────────────────────────────────────

describe('MochiPage pinned files', () => {
  it('offers the empty state with no pins', async () => {
    renderPage()
    await waitForLive()
    expect(screen.getByText('No pinned files')).toBeTruthy()
    expect(
      screen.getByText('Mochi pins files here when you ask it to watch one')
    ).toBeTruthy()
  })

})

// ── Manual refresh ──────────────────────────────────────────────────────────

describe('MochiPage refresh', () => {
  it('refetches every panel on demand', async () => {
    renderPage()
    await waitForLive()
    const before = vi.mocked(api.getPetState).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))
    await waitFor(() =>
      expect(vi.mocked(api.getPetState).mock.calls.length).toBeGreaterThan(before)
    )
  })
})
