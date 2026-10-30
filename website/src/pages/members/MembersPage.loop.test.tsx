import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, cleanup, screen, waitFor, within, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* Same api mock shape as MembersPage.filters.test.tsx. */
vi.mock('../../api/client', () => ({
  api: {
    members: vi.fn(),
    teams: { list: vi.fn(() => Promise.resolve({ teams: [] })) },
    memberThread: vi.fn((slug: string) =>
      Promise.resolve({ slot_key: 'member-' + slug, slug, member: slug, created: true }),
    ),
    memberActivity: vi.fn(() => Promise.resolve({ slug: '', member: '', capped: false, entries: [] })),
    crons: vi.fn(() => Promise.resolve({ jobs: [] })),
    webhooks: vi.fn(() => Promise.resolve({ tokens: [] })),
    defaultAgent: vi.fn(() => Promise.resolve({ default_agent: '' })),
    autonudgeList: vi.fn(() => Promise.resolve({ enabled: true, loops: [] })),
    autonudgeForSlot: vi.fn(() => Promise.resolve({ enabled: true, loop: null })),
    monitorForSlot: vi.fn(() => Promise.resolve({ enabled: true, monitor: null })),
    autonudgeSetActive: vi.fn(),
    crewBoard: vi.fn(),
  },
}))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'
import { normalizeAutomationRecord } from '../../monitoring/automation'
import { sseAutomation } from '../../store/chatSlice'
import { structuredMonitorLoop } from '../../test/monitorFixtures'

const LABEL = 'On watch'

function row(name: string) {
  return {
    name, slug: name, slot_key: `member-${name}`, running: false, kiro_agent: name,
    workspace: 'default', memory_store: 'default', model: '', source: 'kirocrew', starred: false,
  }
}

const monitor = (slotKey: string, patch: Record<string, unknown> = {}) => ({
  ...structuredMonitorLoop(patch),
  id: `monitor-${slotKey}`,
  slot_key: slotKey,
})

/** A conductor's own patrol: the `work-ledger` goal loop, as the REST row carries it. */
const ledgerLoop = (slotKey: string, patch: Record<string, unknown> = {}) => ({
  id: `ledger-${slotKey}`, slot_key: slotKey, message: 'patrol', created_ts: 1_799_000_000,
  idle_secs: 900, max_cycles: 1000, cycle_count: 175, active: true,
  last_fire_ts: 1_799_999_880, next_due_ts: 1_800_000_780, max_runtime_secs: 604_800,
  stopped_reason: '', monitor_outcome: '', monitor_kind: 'work-ledger', ...patch,
})

const boardItem = (id: string, terminal = false) => ({
  schema: 1, item_id: id, title: id, acceptance: {}, state: terminal ? 'closed' : 'open', verdict: terminal ? 'accepted' : '',
  decision: '', round: 1, fails: 0, status: 'progress', summary: '', artifacts: {}, pr: null,
  last_report_at: null, created_at: '2026-10-23T00:00:00Z', closed_at: null, decided_at: null,
  orphaned: false, stale: false, acceptance_concrete: true, outstanding: false, terminal, alive: 'running', events: [],
})

async function renderPage(route = '/members') {
  ;(api.members as ReturnType<typeof vi.fn>).mockResolvedValue({ members: [row('alpha'), row('beta')] })
  const utils = renderWithProviders(<MembersPage />, { route })
  await within(await screen.findByTestId('member-roster')).findByText('alpha')
  return utils
}

const rosterIndicators = () =>
  Array.from(document.querySelectorAll('[data-testid="member-loop-indicator"]'))

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  // clearAllMocks keeps implementations: reset the one a case overrides.
  vi.mocked(api.autonudgeList).mockResolvedValue({ enabled: true, loops: [] } as never)
  vi.mocked(api.autonudgeForSlot).mockResolvedValue({ enabled: true, loop: null } as never)
  vi.mocked(api.monitorForSlot).mockResolvedValue({ enabled: true, monitor: null } as never)
  vi.mocked(api.crewBoard).mockResolvedValue({
    conductor: { schema: 1, slot_key: 'member-alpha', goal: '', round: 1, depth: 0, parent_item: null, created_at: '2026-10-23T00:00:00Z' },
    conductor_alive: 'idle', items: [boardItem('a'), boardItem('b'), boardItem('c', true)], take_over_available: false,
  } as never)
})

describe('MembersPage — on-watch loop indicator', () => {
  it('a live structured monitor marks every avatar surface, then stops at terminal state', async () => {
    const { store } = await renderPage('/members?member=beta')
    const live = normalizeAutomationRecord(monitor('member-beta'))!
    act(() => { store.dispatch(sseAutomation(live)) })
    await waitFor(() => expect(rosterIndicators()).toHaveLength(1))
    const rosterMark = rosterIndicators()[0]
    expect(rosterMark.closest('li')?.textContent).toContain('beta')
    expect(rosterMark.getAttribute('aria-hidden')).toBe('true')
    expect(rosterMark.getAttribute('title')).toBe(LABEL)
    expect(rosterMark.closest('li')?.querySelector('[data-testid="member-loop-label"]')?.textContent).toBe(LABEL)
    const pill = await screen.findByTestId('member-identity-pill')
    expect(within(pill).getByTestId('member-pill-loop-indicator')).toBeInTheDocument()

    const stopped = normalizeAutomationRecord(monitor('member-beta', {
      active: false,
      outcome: 'success',
      stopped_reason: 'monitor_terminal',
      stopped_at: 1,
    }))!
    act(() => { store.dispatch(sseAutomation(stopped)) })
    await waitFor(() => expect(rosterIndicators()).toHaveLength(0))
    expect(within(pill).queryByTestId('member-pill-loop-indicator')).toBeNull()
  })

  it('an active legacy goal loop shows the same label, never its cycle count', async () => {
    vi.mocked(api.autonudgeList).mockResolvedValue({
      enabled: true,
      loops: [{ id: 'l1', slot_key: 'member-alpha', active: true, cycle_count: 3, max_cycles: 24, message: 'nudge text' }],
    } as never)
    await renderPage()
    await waitFor(() => expect(rosterIndicators()).toHaveLength(1))
    expect(rosterIndicators()[0].getAttribute('title')).toBe(LABEL)
    expect(document.body.textContent).not.toMatch(/3 of 24|On patrol/)
  })

  it('reads a known roster slot while the thread open is still pending', async () => {
    vi.mocked(api.memberThread).mockImplementationOnce(() => new Promise(() => undefined))
    await renderPage('/members?member=alpha')
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    await screen.findByTestId('crew-profile-panel')
    await waitFor(() => expect(api.monitorForSlot).toHaveBeenCalledWith('member-alpha'))
  })

  it('fails closed when the slot snapshot cannot be read: no avatar mark, no patrol row, and it says so', async () => {
    vi.mocked(api.monitorForSlot).mockRejectedValue(new Error('owner read failed'))
    const { store } = await renderPage('/members?member=alpha')
    act(() => { store.dispatch(sseAutomation(normalizeAutomationRecord(ledgerLoop('member-alpha'))!)) })
    const pill = await screen.findByTestId('member-identity-pill')
    expect(await within(pill).findByTestId('member-pill-loop-indicator')).toBeInTheDocument()
    fireEvent.click(pill)
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card).getByRole('tab', { name: 'Schedules' }))
    await waitFor(() => expect(within(card).queryByTestId('crew-profile-loop-indicator')).toBeNull())
    expect(within(card).queryByTestId('crew-patrol-row')).toBeNull()
    // Silence here would read as "nothing wakes this mate": the failure is said instead.
    expect(await within(card).findByTestId('crew-patrol-read-error')).toHaveTextContent('Could not read what wakes this mate on its own.')
    expect(within(card).getByTestId('crew-schedule-heading-self')).toBeInTheDocument()
  })

  it('a board read that fails for a reason other than "no ledger" says so under the row; a 404 is a quiet gap', async () => {
    vi.mocked(api.crewBoard).mockRejectedValue(Object.assign(new Error('boom'), { status: 500 }))
    const { store } = await renderPage('/members?member=alpha')
    act(() => { store.dispatch(sseAutomation(normalizeAutomationRecord(ledgerLoop('member-alpha'))!)) })
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card).getByRole('tab', { name: 'Schedules' }))
    const row = await within(card).findByTestId('crew-patrol-row')
    expect(await within(card).findByTestId('crew-patrol-count-error')).toHaveTextContent('Could not count its open tasks.')
    expect(within(row).getByTestId('crew-patrol-facts')).not.toHaveTextContent('tasks open')

    cleanup()
    vi.mocked(api.crewBoard).mockRejectedValue(Object.assign(new Error('no ledger'), { status: 404 }))
    const second = await renderPage('/members?member=alpha')
    act(() => { second.store.dispatch(sseAutomation(normalizeAutomationRecord(ledgerLoop('member-alpha'))!)) })
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    const card2 = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card2).getByRole('tab', { name: 'Schedules' }))
    await within(card2).findByTestId('crew-patrol-row')
    await waitFor(() => expect(api.crewBoard).toHaveBeenCalled())
    expect(within(card2).queryByTestId('crew-patrol-count-error')).toBeNull()
  })

  it('a pull request watch marks the avatar but is no row on the Schedules tab', async () => {
    const { store } = await renderPage('/members?member=alpha')
    act(() => { store.dispatch(sseAutomation(normalizeAutomationRecord(monitor('member-alpha'))!)) })
    const pill = await screen.findByTestId('member-identity-pill')
    await within(pill).findByTestId('member-pill-loop-indicator')
    fireEvent.click(pill)
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card).getByRole('tab', { name: 'Schedules' }))
    await within(card).findByTestId('crew-schedule-list')
    expect(within(card).queryByTestId('crew-patrol-row')).toBeNull()
    // The header stays: the Heartbeat row (#23280) sits under it on every mate.
    expect(within(card).queryByTestId('crew-patrol-error')).toBeNull()
  })

  it('the Schedules tab shows the patrol from the same live record, and Pause writes through the api', async () => {
    vi.mocked(api.autonudgeSetActive).mockResolvedValue({ loop: ledgerLoop('member-alpha', { active: false, stopped_reason: 'manual' }) } as never)
    const { store } = await renderPage('/members?member=alpha')
    act(() => { store.dispatch(sseAutomation(normalizeAutomationRecord(ledgerLoop('member-alpha'))!)) })
    const pill = await screen.findByTestId('member-identity-pill')
    await within(pill).findByTestId('member-pill-loop-indicator')
    fireEvent.click(pill)
    const card = await screen.findByTestId('crew-profile-panel')
    expect(within(card).getByTestId('crew-profile-loop-indicator')).toBeInTheDocument()
    fireEvent.click(within(card).getByRole('tab', { name: 'Schedules' }))
    const row = await within(card).findByTestId('crew-patrol-row')
    expect(row).toHaveAttribute('data-state', 'running')
    expect(within(card).getByTestId('crew-schedule-heading-self')).toHaveTextContent('On its own')
    expect(within(card).getByTestId('crew-schedule-heading-yours')).toHaveTextContent('Your schedules')
    // The board count arrives from the masked crew-board read.
    await waitFor(() => expect(within(row).getByTestId('crew-patrol-facts')).toHaveTextContent('2 tasks open'))
    // No budget bookkeeping anywhere on the card.
    expect(card.textContent).not.toMatch(/175|1,000|Runtime started|Time used|Stopped because/)
    fireEvent.click(within(row).getByTestId('crew-patrol-toggle'))
    await waitFor(() => expect(api.autonudgeSetActive).toHaveBeenCalledWith('ledger-member-alpha', false))
  })

  it('a Pause still in flight holds the Schedules tab: the rail refuses the switch until the write settles', async () => {
    let settle: (v: unknown) => void = () => undefined
    vi.mocked(api.autonudgeSetActive).mockImplementation(() => new Promise((resolve) => { settle = resolve }))
    const { store } = await renderPage('/members?member=alpha')
    act(() => { store.dispatch(sseAutomation(normalizeAutomationRecord(ledgerLoop('member-alpha'))!)) })
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card).getByRole('tab', { name: 'Schedules' }))
    const row = await within(card).findByTestId('crew-patrol-row')
    fireEvent.click(within(row).getByTestId('crew-patrol-toggle'))
    await waitFor(() => expect(within(row).getByTestId('crew-patrol-toggle')).toBeDisabled())
    // The switch is refused while the PATCH is out: the row, and the refusal it
    // may come back with, stay mounted.
    fireEvent.click(within(card).getByRole('tab', { name: 'Profile' }))
    expect(card).toHaveAttribute('data-tab', 'schedule')
    expect(within(card).getByTestId('crew-patrol-row')).toBeInTheDocument()
    await act(async () => { settle({ loop: ledgerLoop('member-alpha', { active: false, stopped_reason: 'manual' }) }) })
    await waitFor(() => expect(within(card).getByTestId('crew-patrol-toggle')).toBeEnabled())
    fireEvent.click(within(card).getByRole('tab', { name: 'Profile' }))
    await waitFor(() => expect(card).toHaveAttribute('data-tab', 'profile'))
  })

  it('a refused Pause stays with the crewmate it belongs to: switching crewmates shows no error under the other row', async () => {
    vi.mocked(api.autonudgeSetActive).mockRejectedValue(new Error('loop is mid-fire'))
    const { store } = await renderPage('/members?member=alpha')
    act(() => {
      store.dispatch(sseAutomation(normalizeAutomationRecord(ledgerLoop('member-alpha'))!))
      store.dispatch(sseAutomation(normalizeAutomationRecord(ledgerLoop('member-beta'))!))
    })
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card).getByRole('tab', { name: 'Schedules' }))
    fireEvent.click(within(await within(card).findByTestId('crew-patrol-row')).getByTestId('crew-patrol-toggle'))
    expect(await within(card).findByTestId('crew-patrol-error')).toHaveTextContent('loop is mid-fire')

    // The roster stays beside the thread here; pick beta from it.
    fireEvent.click(within(screen.getByTestId('member-roster')).getByText('beta'))
    await waitFor(() => expect(screen.getByTestId('member-title-row')).toHaveTextContent('beta'))
    fireEvent.click(await screen.findByTestId('member-identity-pill'))
    const betaCard = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(betaCard).getByRole('tab', { name: 'Schedules' }))
    const betaRow = await within(betaCard).findByTestId('crew-patrol-row')
    expect(betaRow).toHaveAttribute('data-state', 'running')
    expect(within(betaCard).queryByTestId('crew-patrol-error')).toBeNull()
    expect(within(betaRow).getByTestId('crew-patrol-toggle')).toBeEnabled()
  })
})
