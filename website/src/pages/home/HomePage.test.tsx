// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { api } from '../../api/client'
import { createTestStore, renderWithProviders } from '../../test/helpers'
import { sseStatus } from '../../store/dashboardSlice'
import HomePage from './HomePage'
import { Route, Routes } from 'react-router-dom'

// Whether the provider reports usage; one test turns it off.
const usageCaps = vi.hoisted(() => ({ usageBilling: true }))
const DAY = '2026-11-17'
vi.mock('../../providers', () => ({
  useProvider: () => ({
    id: 'test',
    displayName: 'Test Provider',
    capabilities: usageCaps,
    fetchUsage: vi.fn().mockResolvedValue({
      sessions: { total: 10, today: { sessions: 4, messages: 31, toolCalls: 2 }, thisWeek: {}, thisMonth: {}, avgMsgsPerSession: 1, dailyHistory: [] },
      billing: null, tokens: { total: 1 }, costUsd: 9,
      tokenDailyHistory: [{ date: DAY, input: 1_000, output: 200, cacheCreate: 0, cacheRead: 0, costUsd: 2.5, models: {
        sonnet: { input: 800, output: 150, cacheCreate: 0, cacheRead: 0, costUsd: 2 },
        haiku: { input: 200, output: 50, cacheCreate: 0, cacheRead: 0, costUsd: 0.5 },
      } }],
      localDay: DAY,
    }),
  }),
}))

const NOW = Date.parse(`${DAY}T12:00:00Z`)

function store() {
  const initial = createTestStore().getState()
  return createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: [
    { key: 'slot-0', title: 'Build', messages: 2, running: true, last_turn_ts: `${DAY}T11:48:00Z`, last_message: 'Writing the tests' },
    { key: 'slot-1', title: 'Review', messages: 2, running: false, needs_input: true },
    { key: 'slot-2', title: 'Approval one', messages: 2, running: false },
    { key: 'slot-3', title: 'Parked', messages: 2, running: false },
    { key: 'slot-4', title: 'Roadmap update', messages: 2, running: false, has_options: true, options: ['Do x', 'Do y'], options_ts: `${DAY}T11:00:00Z` },
    { key: 'slot-5', title: 'Lunch planning', messages: 2, running: false, has_options: true, options: ['Do z'], options_ts: `${DAY}T11:00:00Z` },
  ] } })
}

const tile = (id: string) => within(screen.getByTestId(id))
const value = (id: string) => tile(id).findByTestId('stat-card-value')

describe('HomePage stat strip', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(NOW)
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ agent: { acp_backend: 'claude' } })
    vi.spyOn(api, 'pendingQuestions').mockResolvedValue([{ slot: 'slot-1', ask_id: 'q1', questions: [{ question: 'Which?', options: [{ label: 'A' }] }] }])
    vi.spyOn(api, 'approvals').mockResolvedValue([{ id: 'ap-2', instance: 'i', slot: 'slot-2', tool: 'shell', tool_input: 'ls' }])
    vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs: [{ run_id: 'wf', name: 'Nightly', session_key: 'slot-0', status: 'running' }] })
    // Two finished turns in the window for the running session; nothing for the others.
    vi.spyOn(api, 'usageTurns').mockImplementation(async (slot: string) => ({ slot, days: 0.5, turns: slot === 'slot-0'
      ? [{ ts: `${DAY}T06:00:00Z`, duration_ms: 3_600_000 }, { ts: `${DAY}T10:00:00Z`, duration_ms: 600_000 }] : [] }))
    vi.spyOn(api, 'members').mockResolvedValue({ members: [
      { name: 'Scout', slug: 'scout', slot_key: 'member-scout', running: false, last_active_ts: NOW / 1000 - 7200 },
      { name: 'Builder', slug: 'builder', slot_key: 'member-builder', running: true, last_active_ts: NOW / 1000 - 60 },
    ] })
    vi.spyOn(api, 'crons').mockResolvedValue({ jobs: [
      { id: 'a', name: 'Digest', message: '', enabled: true, schedule: 'daily', last_status: 'ok', next_run_ts: NOW / 1000 + 3600 },
      { id: 'b', name: 'Broken', message: '', enabled: true, schedule: 'daily', last_status: 'error', next_run_ts: NOW / 1000 + 7200 },
      { id: 'c', name: 'Paused', message: '', enabled: false, schedule: 'daily', last_status: 'ok' },
    ] })
  })
  afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); usageCaps.usageBilling = true })

  it('counts running work, what needs a person, enabled jobs and the day from the shared sources', async () => {
    renderWithProviders(<HomePage />, { store: store() })
    expect(screen.getByTestId('page-title')).toHaveTextContent('Home')
    // One running session and its running workflow; the question and the approval
    // are two items that need a person.
    expect(await value('home-stat-active')).toHaveTextContent('2')
    expect(tile('home-stat-active').getByTestId('stat-card-detail')).toHaveTextContent('1 session, 0 subagents, and 1 workflow')
    // Two blockers (the question, the approval); the two `[OPTIONS:]` offers are
    // suggestions and do not count.
    expect(await value('home-stat-attention')).toHaveTextContent('2')
    expect(tile('home-stat-attention').getByTestId('stat-card-detail')).toHaveTextContent('1 question and 1 approval · 2 suggestions')
    // Two of three jobs are enabled; one of them failed, which colours the tile.
    const schedule = await value('home-stat-schedule')
    expect(schedule).toHaveTextContent('2')
    expect(schedule).toHaveClass('text-danger-text')
    expect(tile('home-stat-schedule').getByTestId('stat-card-detail')).toHaveTextContent('Next: Digest in 1h · 1 failing · 1 paused')
    // A dollar-billed harness leads with today's cost.
    expect(await value('home-stat-today')).toHaveTextContent('$2.50')
  })

  it('shows the day as sessions on a harness that does not bill in dollars', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    renderWithProviders(<HomePage />, { store: store() })
    expect(await value('home-stat-today')).toHaveTextContent('4')
  })

  it('draws a dash for Today when the provider tracks no usage', async () => {
    usageCaps.usageBilling = false
    renderWithProviders(<HomePage />, { store: store() })
    expect(await value('home-stat-today')).toHaveTextContent('—')
  })

  it('says so when the schedule cannot be read, and keeps the other tiles', async () => {
    vi.mocked(api.crons).mockRejectedValue(new Error('down'))
    renderWithProviders(<HomePage />, { store: store() })
    expect(await screen.findByTestId('home-schedule-error')).toHaveTextContent('The schedule could not be loaded.')
    expect(await value('home-stat-schedule')).toHaveTextContent('—')
    expect(await value('home-stat-active')).toHaveTextContent('2')
  })

  it('lists what needs a person as answerable cards, then the fleet in reading order', async () => {
    const answer = vi.spyOn(api, 'answerQuestion').mockResolvedValue({})
    renderWithProviders(<HomePage />, { store: store() })
    const inbox = await screen.findByTestId('home-needs-you')
    // The question card is the All Dashboards page's own: answering here routes the same way.
    await within(inbox).findByText('Which?')
    // Follow-up offers are rows behind a fold, not cards.
    expect(within(inbox).queryByText('The session is waiting for your choice.')).toBeNull()
    const fold = within(inbox).getByTestId('home-suggestions')
    expect(within(fold).getByText('2 sessions offered next steps')).toBeInTheDocument()
    const offers = within(fold).getAllByTestId('home-suggestion-row')
    expect(offers.map(r => within(r).getByText(/^Offered/).textContent)).toEqual(['Offered 2 next steps', 'Offered 1 next step'])
    expect(within(offers[0]).getByRole('link', { name: /Open/ })).toHaveAttribute('href', '/chat?sid=slot-4')
    expect(within(inbox).getByRole('button', { name: 'Approve once' })).toBeVisible()
    fireEvent.click(within(inbox).getByText('A'))
    fireEvent.click(within(inbox).getByRole('button', { name: 'Send answer' }))
    await waitFor(() => expect(answer).toHaveBeenCalledWith('q1', { 'Which?': 'A' }))
    // Needs-you rows first (the question's session, then the approval's, lifted
    // from idle), then the running session and its workflow; the parked session
    // is at rest.
    const fleet = screen.getByTestId('home-fleet')
    const rows = () => within(screen.getByTestId('home-fleet-active')).getAllByTestId('home-fleet-row')
    await waitFor(() => expect(rows()).toHaveLength(4))
    expect(rows().map(r => r.getAttribute('data-state'))).toEqual(['needs_input', 'needs_input', 'running', 'running'])
    expect(within(rows()[0]).getByRole('link', { name: /Open session/ })).toHaveAttribute('href', '/chat?sid=slot-1')
    expect(within(fleet).getByText('Nightly')).toBeVisible()
    expect(within(rows()[1]).getByText('Approval one')).toBeVisible()
    // The running row says how long it has run and what the agent last said.
    expect(within(rows()[2]).getByTestId('home-fleet-elapsed')).toHaveTextContent('running 12m')
    expect(within(rows()[2]).getByText('Writing the tests')).toBeVisible()
    // The parked session and the two that only offered next steps sit behind the
    // closed "at rest" fold until it is opened; their turns are read only then.
    expect(within(fleet).getByText('Parked')).not.toBeVisible()
    expect(vi.mocked(api.usageTurns)).not.toHaveBeenCalledWith('slot-3', expect.anything())
    fireEvent.click(within(fleet).getByText('3 at rest'))
    expect(within(fleet).getByText('Parked')).toBeVisible()
    await waitFor(() => expect(vi.mocked(api.usageTurns)).toHaveBeenCalledWith('slot-3', expect.anything()))
  })

  it('says the fleet is empty when no session is open', async () => {
    vi.mocked(api.pendingQuestions).mockResolvedValue([])
    vi.mocked(api.approvals).mockResolvedValue([])
    vi.mocked(api.workflowRuns).mockResolvedValue({ runs: [] })
    const initial = createTestStore().getState()
    renderWithProviders(<HomePage />, { store: createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true, slots: [] } }) })
    expect(await screen.findByTestId('home-fleet-empty-title')).toHaveTextContent('No sessions')
    expect(await screen.findByTestId('home-needs-you-empty-title')).toHaveTextContent('Nothing in this category is waiting for you.')
  })

  it('lists bots working first, the next jobs due, and the gateway health', async () => {
    const withStatus = store()
    withStatus.dispatch(sseStatus({ start_time: NOW / 1000 - 3600, version: '0.1.0' } as never))
    renderWithProviders(<HomePage />, { store: withStatus })
    const bots = screen.getByTestId('home-bots')
    const botRows = await within(bots).findAllByTestId('home-bot-row')
    expect(botRows.map(r => r.getAttribute('data-running'))).toEqual(['true', 'false'])
    expect(within(botRows[0]).getByText('Builder')).toBeVisible()
    expect(within(botRows[0]).getByRole('link', { name: /Open/ })).toHaveAttribute('href', '/members?member=Builder')
    expect(within(botRows[1]).getByText('Last active 2h ago')).toBeVisible()
    expect(within(bots).getByText('1 of 2 working')).toBeVisible()
    // Enabled jobs with a forecast, soonest first; the paused one is not due.
    const schedule = screen.getByTestId('home-schedule')
    const jobRows = await within(schedule).findAllByTestId('home-schedule-row')
    expect(jobRows.map(r => within(r).getByText(/Digest|Broken/).textContent)).toEqual(['Digest', 'Broken'])
    expect(within(jobRows[0]).getByText('Due in 1h')).toBeVisible()
    expect(within(jobRows[1]).getByText('Error')).toBeVisible()
    expect(within(jobRows[0]).getByRole('link', { name: /Open/ })).toHaveAttribute('href', '/schedule?job=a')
    expect(screen.getByTestId('gateway-health-line')).toHaveTextContent('All systems running')
    expect(screen.getByTestId('gateway-health-line')).toHaveTextContent('Up 1h 0m 0s · v0.1.0')
  })

  it('says so when the roster cannot be read', async () => {
    vi.mocked(api.members).mockRejectedValue(new Error('down'))
    // The roster read retries on its own (`retryThroughRestart`); no backoff, so
    // the failure lands within the test's wait.
    renderWithProviders(<HomePage />, { store: store(), queryDefaults: { retryDelay: 0 } })
    expect(await screen.findByTestId('home-bots-error')).toHaveTextContent('The bots could not be loaded.')
  })

  it.each([
    ['home-stat-active', '/session-dashboards'],
    ['home-stat-schedule', '/schedule'],
    ['home-stat-today', '/settings/overview'],
  ])('opens the page a tile summarizes (%s)', async (tile, path) => {
    renderWithProviders(
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path={path} element={<div data-testid="landed">{path}</div>} />
      </Routes>,
      { store: store() },
    )
    fireEvent.click(await screen.findByTestId(tile))
    expect(await screen.findByTestId('landed')).toHaveTextContent(path)
  })

  it('shows blockers a row of three at a time behind a Show more button', async () => {
    const slots = ['slot-0', 'slot-1', 'slot-2', 'slot-3', 'slot-4', 'slot-5', 'slot-6']
    vi.mocked(api.pendingQuestions).mockResolvedValue(slots.map(slot =>
      ({ slot, ask_id: `q-${slot}`, questions: [{ question: `Which for ${slot}?`, options: [{ label: 'A' }] }] })))
    vi.mocked(api.approvals).mockResolvedValue([])
    const initial = createTestStore().getState()
    renderWithProviders(<HomePage />, { store: createTestStore({ ...initial, dashboard: { ...initial.dashboard, connected: true, slotsLoaded: true,
      slots: slots.map(key => ({ key, title: key, messages: 1, running: false })) } }) })
    const inbox = await screen.findByTestId('home-needs-you')
    await within(inbox).findByText('Which for slot-0?')
    expect(within(inbox).getByText('Which for slot-2?')).toBeVisible()
    expect(within(inbox).queryByText('Which for slot-3?')).toBeNull()
    // Three more, then the last one.
    expect(within(inbox).getByTestId('home-needs-you-more')).toHaveTextContent('Show 3 more')
    fireEvent.click(within(inbox).getByTestId('home-needs-you-more'))
    expect(within(inbox).getByText('Which for slot-5?')).toBeVisible()
    expect(within(inbox).getByTestId('home-needs-you-more')).toHaveTextContent('Show 1 more')
    fireEvent.click(within(inbox).getByTestId('home-needs-you-more'))
    expect(within(inbox).getByText('Which for slot-6?')).toBeVisible()
    expect(within(inbox).queryByTestId('home-needs-you-more')).toBeNull()
  })

  it('draws each session a track: finished turns, the live run, and a dot where it asks', async () => {
    renderWithProviders(<HomePage />, { store: store() })
    const fleet = screen.getByTestId('home-fleet')
    const rowOf = (text: string) => within(fleet).getByText(text).closest('[data-testid="home-fleet-row"]') as HTMLElement
    // The 12h window runs 00:00 to 12:00: the 06:00 turn starts at 50% and spans an
    // hour (8.3%), the 10:00 one at 83.3%; the live run began at 11:48 (98.3%).
    await within(fleet).findByText('Build')
    await waitFor(() => expect(within(rowOf('Build')).getAllByTestId('home-track-span')).toHaveLength(2))
    const running = rowOf('Build')
    const [first, second] = within(running).getAllByTestId('home-track-span')
    expect(parseFloat(first.style.left)).toBeCloseTo(50, 1)
    expect(parseFloat(first.style.width)).toBeCloseTo(8.33, 1)
    expect(parseFloat(second.style.left)).toBeCloseTo(83.33, 1)
    expect(parseFloat(within(running).getByTestId('home-track-live').style.left)).toBeCloseTo(98.33, 1)
    // The session with a question carries the ask marker at "now".
    expect(within(rowOf('Review')).getByTestId('home-track-ask').style.left).toBe('100%')
    // The workflow sits under its session as an indented child row.
    const rows = within(fleet).getAllByTestId('home-fleet-row')
    const child = rowOf('Nightly')
    expect(child).toHaveClass('pl-6')
    expect(rows.indexOf(child)).toBe(rows.indexOf(running) + 1)
    // The axis labels the window and ends at Now.
    expect(within(fleet).getByText('Now')).toBeInTheDocument()
    // The read always covers the widest window, so switching to 6h costs no request;
    // the 06:00 turn now starts at the window's left edge and spans a sixth of it.
    expect(vi.mocked(api.usageTurns)).toHaveBeenCalledWith('slot-0', `since=${(NOW - 24 * 3_600_000) / 1000}&until=${NOW / 1000}`)
    const calls = vi.mocked(api.usageTurns).mock.calls.length
    fireEvent.click(within(fleet).getByTestId('home-window-6'))
    await waitFor(() => expect(parseFloat(within(rowOf('Build')).getAllByTestId('home-track-span')[0].style.width)).toBeCloseTo(16.67, 1))
    expect(within(rowOf('Build')).getAllByTestId('home-track-span')[0].style.left).toBe('0%')
    expect(vi.mocked(api.usageTurns).mock.calls.length).toBe(calls)
  })

  it('splits today by model: cost on a dollar harness, tokens on a credit one, nothing without usage tracking', async () => {
    renderWithProviders(<HomePage />, { store: store() })
    const spend = await screen.findByTestId('home-spend')
    const rows = await within(spend).findAllByTestId('home-spend-row')
    expect(rows.map(r => r.textContent)).toEqual(['sonnet80%$2.00', 'haiku20%$0.50'])
    expect(within(spend).getAllByTestId('home-spend-segment').map(s => s.style.width)).toEqual(['80%', '20%'])
    expect(within(spend).getByText('$2.50')).toBeInTheDocument()
  })

  it('splits today by tokens when the harness does not bill in dollars', async () => {
    vi.mocked(api.kirocrewConfig).mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    renderWithProviders(<HomePage />, { store: store() })
    const spend = await screen.findByTestId('home-spend')
    const rows = await within(spend).findAllByTestId('home-spend-row')
    expect(rows.map(r => r.textContent)).toEqual(['sonnet79%950 tokens', 'haiku20%250 tokens'])
  })

  it('draws no spend panel when the provider tracks no usage', async () => {
    usageCaps.usageBilling = false
    renderWithProviders(<HomePage />, { store: store() })
    await screen.findByTestId('home-stat-strip')
    expect(screen.queryByTestId('home-spend')).toBeNull()
  })
})
