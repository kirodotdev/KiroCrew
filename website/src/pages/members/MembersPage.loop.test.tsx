import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor, within, fireEvent } from '@testing-library/react'
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
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card).getByRole('tab', { name: 'Profile' }))
    await waitFor(() => expect(api.monitorForSlot).toHaveBeenCalledWith('member-alpha'))
  })

  it('fails closed when the Profile monitor snapshot cannot be read', async () => {
    vi.mocked(api.monitorForSlot).mockRejectedValue(new Error('owner read failed'))
    const { store } = await renderPage('/members?member=alpha')
    const live = normalizeAutomationRecord(monitor('member-alpha'))!
    act(() => { store.dispatch(sseAutomation(live)) })
    const pill = await screen.findByTestId('member-identity-pill')
    expect(await within(pill).findByTestId('member-pill-loop-indicator')).toBeInTheDocument()
    fireEvent.click(pill)
    const card = await screen.findByTestId('crew-profile-panel')
    fireEvent.click(within(card).getByRole('tab', { name: 'Profile' }))
    const details = await within(card).findByTestId('crew-profile-monitor')
    await waitFor(() => expect(details).toHaveTextContent('Monitor status unavailable'))
    await waitFor(() => expect(within(card).queryByTestId('crew-profile-loop-indicator')).toBeNull())
    expect(within(details).queryByText('Type')).toBeNull()
  })

  it('the Profile avatar and details use the same active record', async () => {
    const raw = monitor('member-alpha', { created_ts: 1_799_996_400 })
    const stoppedSnapshot = monitor('member-alpha', {
      active: false, outcome: 'success', stopped_reason: 'monitor_terminal', stopped_at: 1_800_000_000,
    })
    vi.mocked(api.monitorForSlot).mockResolvedValue({ enabled: true, monitor: stoppedSnapshot } as never)
    const { store } = await renderPage('/members?member=alpha')
    act(() => { store.dispatch(sseAutomation(normalizeAutomationRecord(raw)!)) })
    const pill = await screen.findByTestId('member-identity-pill')
    const pillMark = await within(pill).findByTestId('member-pill-loop-indicator')
    expect(pillMark.getAttribute('title')).toBe(LABEL)
    expect(screen.getByTestId('member-pill-activity-sr').textContent).toContain(LABEL)

    fireEvent.click(pill)
    const card = await screen.findByTestId('crew-profile-panel')
    expect(within(card).getByTestId('crew-profile-loop-indicator')).toBeInTheDocument()
    fireEvent.click(within(card).getByRole('tab', { name: 'Profile' }))
    const details = within(card).getByTestId('crew-profile-monitor')
    expect(details).toHaveAttribute('data-monitor-active', 'true')
    expect(details).toHaveTextContent('https://github.com/owner/repo/pull/123')
    expect(details).toHaveTextContent('5m')
    expect(details).toHaveTextContent('5 / No limit')
  })
})
