import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, fireEvent, act } from '@testing-library/react'
import { i18next } from '../i18n/all'
import { renderWithProviders, createTestStore } from './helpers'
import { namedCeiling } from './namedCeiling'
import OverviewPage from '../pages/OverviewPage'
import { api } from '../api/client'
import { KIRO_SIGN_IN_BACKEND } from '../pages/developer/kiroSignInLink'
import type { RootState } from '../store'

// Mock the two drill-in surfaces to isolate the mission-control shell.
vi.mock('../pages/overview', () => ({
  UsageTab: () => <div data-testid="usage-tab">UsageTab</div>,
  WakaTimeTab: () => <div data-testid="wakatime-tab">WakaTimeTab</div>,
}))
vi.mock('../pages/overview/MemoryTab', () => ({
  default: () => <div data-testid="memory-tab">MemoryTab</div>,
}))

vi.mock('../hooks/useUptime', () => ({
  useUptime: () => '2h 30m',
}))

vi.mock('../api/client', () => ({
  api: {
    memorySettings: vi.fn().mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false }),
    wakatimeStats: vi.fn().mockResolvedValue({
      configured: true,
      range: 'last_7_days',
      stats: { total_seconds: 1800, total_seconds_including_other_language: 7200 },
    }),
    // Present only to be asserted NEVER called: the landing page must not read
    // the Kiro sign-in status, let alone render its chooser.
    kasLoginStatus: vi.fn().mockResolvedValue({ authenticated: false }),
    // The selected backend decides whether the sign-in signpost renders.
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: 'kiro' } }),
  },
}))

// The Today line's counts, which one test sets to the singular.
const usageToday = vi.hoisted(() => ({ sessions: 3, messages: 42, toolCalls: 5 }))
// The gateway's local day, the key of the token history bucket the Today line
// reads; the report names it, so the browser's own clock and zone play no part.
const todayKey = vi.hoisted(() => '2026-11-11')
// Whether the provider reports usage; one test turns it off.
const usageCaps = vi.hoisted(() => ({ usageBilling: true }))

// Usage summary card goes through the provider seam.
vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'test',
    displayName: 'Test Provider',
    capabilities: usageCaps,
    fetchUsage: vi.fn().mockResolvedValue({
      sessions: {
        total: 10,
        today: { ...usageToday },
        thisWeek: { sessions: 8, messages: 100, toolCalls: 12 },
        thisMonth: { sessions: 10, messages: 120, toolCalls: 15 },
        avgMsgsPerSession: 12,
        dailyHistory: [],
      },
      billing: { plan: 'Pro', percentUsed: 42, unit: 'tokens' },
      // 30-day totals; the Today line reads today's bucket below instead.
      tokens: { total: 900_000 },
      costUsd: 99,
      tokenDailyHistory: [
        { date: '2000-01-01', input: 5, output: 5, cacheCreate: 0, cacheRead: 0, costUsd: 1 },
        { date: todayKey, input: 60_000, output: 3_000, cacheCreate: 200, cacheRead: 100, costUsd: 4.12 },
      ],
      localDay: todayKey,
    }),
  }),
}))

function statusStore(connected = true) {
  return createTestStore({
    dashboard: {
      status: { uptime: '2h', sessions: 3, messages: 42, cron_jobs: 1, subagents: 0, lessons: 5, version: '0.1.0' },
      connected,
      slots: [],
      refreshTrigger: 0,
    } as RootState['dashboard'],
  })
}

describe('OverviewPage — mission control', () => {
  it('renders the health hero and stat tiles, with no nested tab bar', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByText('All systems running')).toBeInTheDocument()
    expect(screen.getByText('Uptime')).toBeInTheDocument()
    expect(screen.getByText('Sessions')).toBeInTheDocument()
    // The landing view has no sub-tab bar.
    expect(screen.queryByText('KiroCrew Config')).not.toBeInTheDocument()
    expect(screen.queryByText('Agent Config')).not.toBeInTheDocument()
    expect(screen.queryByText('Import/Export')).not.toBeInTheDocument()
    // Neither drill-in surface is mounted on the landing view.
    expect(screen.queryByTestId('memory-tab')).not.toBeInTheDocument()
    expect(screen.queryByTestId('usage-tab')).not.toBeInTheDocument()
  })

  it('shows the connecting state without status', () => {
    renderWithProviders(<OverviewPage />)
    expect(screen.getByText('Connecting…')).toBeInTheDocument()
  })

  it('drops the healthy claim when the socket disconnects (stale status kept)', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore(false) })
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument()
    expect(screen.queryByText('All systems running')).not.toBeInTheDocument()
  })

  it('renders usage and memory summary cards', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByText('Usage')).toBeInTheDocument()
    expect(screen.getByText('Memory')).toBeInTheDocument()
    expect(await screen.findByText(/Summarizes chats into memory after 3h idle/)).toBeInTheDocument()
    expect(await screen.findByText(/Today:/)).toBeInTheDocument()
  })

  it('reads the Today line as one phrase with plural counts and a formatted cost', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValueOnce({ agent: { acp_backend: 'claude' } })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Today: 3 sessions · 42 messages · 63.3K tokens · $4.12')).toBeInTheDocument()
  })

  it('leaves tokens and cost off the Today line on a credit-billed harness', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Today: 3 sessions · 42 messages')).toBeInTheDocument()
  })

  it('says so when the config read fails, and keeps the sessions on the Today line', async () => {
    vi.mocked(api.kirocrewConfig).mockRejectedValueOnce(new Error('config unavailable'))
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    // The notice waits on the config read failing and the usage read landing.
    const notice = await screen.findByTestId('overview-usage-harness-unread', {}, namedCeiling('CONFIG_FAIL_THEN_NOTICE', 5000))
    expect(notice).toHaveTextContent('so today’s tokens and cost can’t be shown')
    expect(await screen.findByText('Today: 3 sessions · 42 messages')).toBeInTheDocument()
  })

  it('uses the singular for a count of one on the Today line', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValueOnce({ agent: { acp_backend: 'claude' } })
    usageToday.sessions = 1
    usageToday.messages = 1
    try {
      renderWithProviders(<OverviewPage />, { store: statusStore() })
      expect(await screen.findByText('Today: 1 session · 1 message · 63.3K tokens · $4.12')).toBeInTheDocument()
    } finally {
      usageToday.sessions = 3
      usageToday.messages = 42
    }
  })

  it('drills into Memory and back', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    // Summary cards share the verb, in render order: Usage (0), WakaTime (1),
    // Memory (2).
    fireEvent.click(screen.getAllByRole('button', { name: /View details/ })[2])
    expect(await screen.findByTestId('memory-tab')).toBeInTheDocument()
    expect(screen.queryByText('All systems running')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    expect(screen.getByText('All systems running')).toBeInTheDocument()
    expect(screen.queryByTestId('memory-tab')).not.toBeInTheDocument()
  })

  it('lets a member drill-in own its title while keeping one back action', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore(), route: '/settings/overview?view=memory&store=member-alice' })
    expect(await screen.findByTestId('memory-tab')).toBeInTheDocument()
    expect(screen.queryByText('Memory')).toBeNull()
    expect(screen.getAllByRole('button', { name: /Overview/ })).toHaveLength(1)
  })

  it('drills into Usage and back', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    fireEvent.click(screen.getAllByRole('button', { name: /View details/ })[0])
    expect(screen.getByTestId('usage-tab')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    expect(screen.queryByTestId('usage-tab')).not.toBeInTheDocument()
  })

  it('shows the WakaTime summary total from the include-other field, not the classified-only total', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    // Fixture: classified total_seconds=1800 (30m), include-other=7200 (2h).
    // The card must read the include-other total so it matches the drill-in.
    expect(await screen.findByText('2h')).toBeInTheDocument()
    expect(screen.queryByText('30m')).not.toBeInTheDocument()
  })

  it('drills into WakaTime and back', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    fireEvent.click(screen.getAllByRole('button', { name: /View details/ })[1])
    expect(screen.getByTestId('wakatime-tab')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    expect(screen.queryByTestId('wakatime-tab')).not.toBeInTheDocument()
  })

  // Overview reads state and edits nothing, so it offers no apply/restart
  // action: the button that used to sit here had no change to apply, and its
  // one effect (dropping live agent sessions) belongs with the surfaces that
  // edit the config those sessions load. Asserted so it is not re-added.
  it('offers no restart action in the hero', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.queryByRole('button', { name: /Restart/i })).not.toBeInTheDocument()
  })

  // The region below the summary cards is empty in the stock build, and the
  // panel seam is only worth anything if the page actually READS it — a
  // registry nothing renders is the failure this asserts against.
  it('renders nothing in the lower panel region until a panel is registered', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.queryByTestId('edition-panel')).not.toBeInTheDocument()
  })

  it('renders a registered lower panel', async () => {
    const { registerOverviewPanel } = await import('../pages/overviewPanel')
    registerOverviewPanel({
      id: 'test:lower-panel',
      component: () => <div data-testid="edition-panel">registered</div>,
    })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByTestId('edition-panel')).toBeInTheDocument()
  })

  // The Kiro sign-in card serves the KAS backend only, so it lives under the
  // switch that picks KAS, on Settings > Agent Harness. On the landing page
  // its provider chooser read as a required step to every user, first-run
  // installs included. Asserted so it is not re-added: neither the card nor a
  // read of its status belongs here.
  it('hosts no Kiro sign-in card and never reads the sign-in status', async () => {
    const { api } = await import('../api/client')
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Memory')).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-sign-in-card')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Continue with/ })).not.toBeInTheDocument()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })

  it('carries no "sign-in moved" signpost either, whichever backend is selected', async () => {
    const { api } = await import('../api/client')
    // The time-boxed pointer to Settings > Agent Harness is retired: the page
    // neither renders it nor reads the backend to decide whether to.
    const first = renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Memory')).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-sign-in-moved')).not.toBeInTheDocument()
    first.unmount()

    vi.mocked(api.kirocrewConfig).mockResolvedValueOnce({ agent: { acp_backend: KIRO_SIGN_IN_BACKEND } })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Memory')).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-sign-in-moved')).not.toBeInTheDocument()
    expect(screen.queryByText(/sign-in moved/i)).not.toBeInTheDocument()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })
})

// The Memory card line was five catalog fragments glued around two raw
// numbers, so every language kept English word order and "1 days" (#32430).
describe('OverviewPage — Memory card line', () => {
  afterEach(async () => {
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('reads as one sentence with a plural day count', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.memorySettings).mockResolvedValueOnce({ history_idle_hours: 2, history_max_days: 1, migrated: false })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Summarizes chats into memory after 2h idle · keeps 1 day of history')).toBeInTheDocument()
  })

  it('reads as one sentence once semantic memory is on', async () => {
    const { api } = await import('../api/client')
    vi.mocked(api.memorySettings).mockResolvedValueOnce({ history_idle_hours: 3, history_max_days: 90, migrated: true })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Summarizes chats into memory after 3h idle · semantic memory active')).toBeInTheDocument()
  })

  it('follows the word order of the language, not English', async () => {
    await act(async () => { await i18next.changeLanguage('ja') })
    const { api } = await import('../api/client')
    vi.mocked(api.memorySettings).mockResolvedValueOnce({ history_idle_hours: 3, history_max_days: 90, migrated: false })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('3 時間使われないとチャットをメモリに要約 · 90 日分の履歴を保持')).toBeInTheDocument()
  })
})

// The health line, the Usage card's no-usage note and the WakaTime line were
// catalog fragments glued around a value in English order (#32431).
describe('OverviewPage — whole-sentence lines', () => {
  afterEach(async () => {
    usageCaps.usageBilling = true
    await act(async () => { await i18next.changeLanguage('en') })
  })

  /** The element whose own text, children included, is exactly `text`. */
  const line = (tag: string, text: string) => (_: string, el: Element | null) =>
    el?.tagName === tag && el.textContent === text

  it('reads the health line as one phrase in English', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByText(line('DIV', 'Up 2h 30m · v0.1.0'))).toBeInTheDocument()
  })

  it('puts the uptime where the language puts it, and says running in German', async () => {
    await act(async () => { await i18next.changeLanguage('hi') })
    const hindi = renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByText(line('DIV', '2h 30m से चालू · v0.1.0'))).toBeInTheDocument()
    hindi.unmount()

    await act(async () => { await i18next.changeLanguage('de') })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByText(line('DIV', 'Läuft seit 2h 30m · v0.1.0'))).toBeInTheDocument()
  })

  it('names the provider inside the sentence when usage is not tracked', async () => {
    usageCaps.usageBilling = false
    await act(async () => { await i18next.changeLanguage('ja') })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByText('Test Provider では使用量のトラッキングを利用できません。')).toBeInTheDocument()
  })

  it('keeps the WakaTime total styled inside its sentence', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    const total = await screen.findByText('2h')
    expect(total).toHaveClass('font-mono')
    expect(total.parentElement).toHaveTextContent(/^Last 7 days 2h$/)
  })
})

// The status tiles printed the raw JS number, so Bengali read Latin digits
// beside translated labels (#33237).
describe('OverviewPage — status tile counts', () => {
  afterEach(async () => {
    await act(async () => { await i18next.changeLanguage('en') })
  })

  it('writes the counts in the reader\'s digits and keeps an unknown count loading', async () => {
    await act(async () => { await i18next.changeLanguage('bn') })
    const store = createTestStore({
      dashboard: {
        status: { uptime: '2h', sessions: 3, messages: 42, cron_jobs: 1, subagents: 0, lessons: null, version: '0.1.0' },
        connected: true,
        slots: [],
        refreshTrigger: 0,
      } as RootState['dashboard'],
    })
    renderWithProviders(<OverviewPage />, { store })
    // Uptime comes from the mocked hook; lessons is unknown, so it stays a skeleton.
    expect(screen.getAllByTestId('stat-card-value').map(v => v.textContent)).toEqual(['2h 30m', '৩', '৪২', '১', '০'])
    expect(screen.getAllByTestId('stat-card-skeleton')).toHaveLength(1)
  })
})
