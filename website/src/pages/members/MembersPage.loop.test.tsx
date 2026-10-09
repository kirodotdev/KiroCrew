import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, waitFor, within } from '@testing-library/react'
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
    memberPanel: vi.fn(() => Promise.resolve({ panel: null, html: null })),
  },
}))

vi.mock('../../components/ChatPane', () => ({
  default: ({ slotKey }: { slotKey: string }) => <div data-testid="chat-pane-stub">{slotKey}</div>,
}))

import { api } from '../../api/client'
import MembersPage from './MembersPage'
import { sseAutomation } from '../../store/chatSlice'
import type { StructuredMonitor } from '../../monitoring/automation'

const LABEL = 'On watch'

function row(name: string) {
  return {
    name, slug: name, slot_key: `member-${name}`, running: false, kiro_agent: name,
    workspace: 'default', memory_store: 'default', model: '', source: 'kirocrew', starred: false,
  }
}

const monitor = (slotKey: string, overrides: Partial<StructuredMonitor> = {}): StructuredMonitor => ({
  kind: 'structured_monitor', id: `monitor-${slotKey}`, slotKey, active: true,
  actionable: true, version: 1, monitorKind: 'github_pull_request', objective: 'review_ready',
  target: 'https://github.com/kirodotdev/KiroCrew/pull/42', cadenceSecs: 300,
  nextProbeAt: 0, wakeInstructions: '',
  budgets: { maxRuntimeSecs: 14_400, maxAgentTurns: 8, maxTokens: 250_000, maxProviderErrors: 3 },
  latest: { classification: 'unchanged', reasonCode: '', observedAt: 0, decision: 'stay_quiet' },
  usage: { probes: 5, wakes: 2, agentTurns: 1, inputTokens: 100, outputTokens: 20, providerErrors: 0, tokenUsageKnown: true },
  action: { wakeInFlight: false, wakeDelivery: '' }, terminal: null,
  ...overrides,
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
})

describe('MembersPage — on-watch loop indicator', () => {
  it('a live monitor (Redux, from WS frames) marks only that crewmate, with no loop details', async () => {
    const { store } = await renderPage()
    expect(rosterIndicators()).toHaveLength(0)
    act(() => { store.dispatch(sseAutomation(monitor('member-beta'))) })
    await waitFor(() => expect(rosterIndicators()).toHaveLength(1))
    const mark = rosterIndicators()[0]
    expect(mark.closest('li')?.textContent).toContain('beta')
    expect(mark.getAttribute('aria-hidden')).toBe('true')
    expect(mark.getAttribute('title')).toBe(LABEL)
    // A visible word names it, so touch and reduced-motion readers get it too.
    expect(mark.closest('li')?.querySelector('[data-testid="member-loop-label"]')?.textContent).toBe(LABEL)
    // Turns off when the monitor finishes.
    act(() => {
      store.dispatch(sseAutomation(monitor('member-beta', { terminal: { outcome: 'success', reason: '', stoppedAt: 1 } })))
    })
    await waitFor(() => expect(rosterIndicators()).toHaveLength(0))
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

  it('the open thread header shows it too, keeping the pill named by the crewmate', async () => {
    const { store } = await renderPage('/members?member=alpha')
    const pill = await screen.findByTestId('member-identity-pill')
    expect(within(pill).queryByTestId('member-pill-loop-indicator')).toBeNull()
    act(() => { store.dispatch(sseAutomation(monitor('member-alpha'))) })
    const mark = await within(pill).findByTestId('member-pill-loop-indicator')
    // Decorative inside the button; the screen-reader line carries the label.
    expect(mark.getAttribute('aria-hidden')).toBe('true')
    expect(mark.getAttribute('title')).toBe(LABEL)
    expect(screen.getByTestId('member-pill-activity-sr').textContent).toContain(LABEL)
    expect(screen.getByTestId('member-pill-activity').textContent).toContain(LABEL)
  })

  it('the header pill is compact at rest: words open only on hover or keyboard focus', async () => {
    await renderPage('/members?member=alpha')
    const pill = await screen.findByTestId('member-identity-pill')
    const fold = within(pill).getByTestId('member-pill-text')
    // Folded to a zero-width column at rest; opened on hover, keyboard focus
    // or a screen with no hover; the width change animates, cut under reduced motion.
    expect(fold.className).toContain('grid-cols-[0fr]')
    expect(fold.className).toContain('group-hover:grid-cols-[1fr]')
    expect(fold.className).toContain('group-focus-visible:grid-cols-[1fr]')
    expect(fold.className).toContain('[@media(hover:none)]:grid-cols-[1fr]')
    expect(fold.className).toContain('transition-[grid-template-columns]')
    expect(fold.className).toContain('motion-reduce:transition-none')
    expect((fold.firstElementChild as HTMLElement).className).toContain('overflow-hidden')
    expect(pill.className).toMatch(/(^| )group( |$)/)
    // Folded words stay in the DOM: the crewmate still names the button.
    expect(pill).toHaveAccessibleName(expect.stringContaining('alpha'))
    // The door cue stays at rest, outside the fold.
    const chevron = within(pill).getByTestId('member-identity-pill-chevron')
    expect(fold.contains(chevron)).toBe(false)
  })
})
