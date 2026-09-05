/**
 * Regression test: the notification panel asks the page on screen before it
 * jumps away from it.
 *
 * The bell is reachable from every page, including one holding an unsaved draft,
 * and every jump out of its popover replaces that page — the footer's inbox
 * link, the error fallback's, and each of the detail panel's own buttons. None of
 * them asked, so the draft the sidebar and the palette both defend was lost by
 * clicking a notification instead. That is the failure mode #8010 names: the ask
 * is opt-in per surface, and forgetting it fails silently.
 *
 * Pinned against the REAL bell, feed and detail panel rather than a stand-in,
 * because the defect was in these specific call sites. They are wired by TAKING
 * every jump through `useGuardedLeave`, so the assertions below are also
 * what stops a future button in the same panel from shipping unguarded.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import App from '../App'
import SidePanelLayout, { useSidePanelLeaveGuard } from '../components/SidePanelLayout'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import type { Notification } from '../types'

// Same isolation as the other App nav tests: stub the routed pages and the api
// client so App mounts without real network.
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => <div data-testid="inbox-page">inbox</div> }))
vi.mock('../pages/SchedulePage', () => ({ default: () => <div data-testid="schedule-page">schedule</div> }))
// The page at risk, with the REAL SidePanelLayout inside it: the pane is mounted
// conditionally on `?tab=`, and its draft lives in component-local state, as
// PromptsTab's does.
vi.mock('../pages/CapabilitiesPage', () => {
  function DraftPane() {
    const [draft, setDraft] = React.useState('')
    useSidePanelLeaveGuard(() => !draft || confirm('Discard unsaved changes?'), !!draft)
    return (
      <input
        aria-label="draft"
        value={draft}
        onChange={e => setDraft((e.target as HTMLInputElement).value)}
      />
    )
  }
  function CapabilitiesPageStub() {
    return (
      <SidePanelLayout
        title="Agent Capabilities"
        tabs={[
          { key: 'drafts', label: 'Drafts', icon: null },
          { key: 'other', label: 'Other', icon: null },
        ]}
        rememberKey="capabilities"
      >
        {(tab: string) => <>
          {tab === 'drafts' && <DraftPane />}
          {tab !== 'drafts' && <div data-testid="capabilities-other">{tab}</div>}
        </>}
      </SidePanelLayout>
    )
  }
  return { default: CapabilitiesPageStub }
})
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
    sessionsUsage: vi.fn().mockResolvedValue({ usage: { credits_used: 0, credits_covered: 0, credits_plan: 10000, resets: '2026-07-01', plan: 'KIRO POWER', cost_usd: 0, overage_rate: '0.04' } }),
    listApps: vi.fn().mockResolvedValue([]),
    system: vi.fn().mockResolvedValue({ mem_used_gb: 4.0, mem_total_gb: 16.0, cpu_pct: 25.0, disk_total_gb: 100.0, disk_free_gb: 60.0 }),
    chatSlotAgent: vi.fn().mockResolvedValue({}),
    chatSlotReasoningEffort: vi.fn().mockResolvedValue({}),
    chatSlotModel: vi.fn().mockResolvedValue({}),
    chatMode: vi.fn().mockResolvedValue({}),
    listInstances: vi.fn().mockResolvedValue({ instances: [], warm_set_cap: 5 }),
  },
  isAuthBannerShown: vi.fn(() => false),
  ApiError: class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  },
}))

/** One note carrying a dashboard-internal deep link, which is what makes the
 *  detail panel render its "Open" jump. Seeded into the store directly: the boot
 *  fetch is owned by the WebSocket first-connect, which is mocked out here. */
const NOTE: Notification = {
  kind: 'system.agent',
  title: 'Backup finished',
  body: 'All good.',
  ts: '2026-09-01T10:00:00Z',
  url: '/schedule',
}

/** A cron note naming a slot, which is what renders the "Continue session"
 *  button — the jump that switches the active slot BEFORE it navigates. */
const CRON_NOTE: Notification = {
  kind: 'cron',
  title: 'Nightly backup finished',
  body: 'All good.',
  ts: '2026-09-01T11:00:00Z',
  job_id: 'job-1234abcd',
  slot: 'slot-from-the-note',
}

const renderDashboard = (store = createTestStore({
  notifications: { items: [NOTE], clearSeq: 0, ackSeq: 0, ackSeqByTs: {} },
})) =>
  renderWithProviders(
    <NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>,
    { route: '/capabilities?tab=drafts', store },
  )

const typeDraft = (value: string) =>
  fireEvent.change(screen.getByLabelText('draft'), { target: { value } })
const draftValue = () => (screen.getByLabelText('draft') as HTMLInputElement).value
const paneReady = () => waitFor(() => expect(screen.getByLabelText('draft')).toBeInTheDocument())
const openBell = () => fireEvent.click(screen.getByRole('button', { name: /^Notifications$/ }))

describe('notification panel navigation leave guard', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('keeps the page on screen when the inbox jump is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await paneReady()
    typeDraft('half-written prompt')
    openBell()
    fireEvent.click(await screen.findByRole('button', { name: /^Open inbox$/ }))
    expect(confirmSpy).toHaveBeenCalled()
    // The pane is still mounted, which is what saves the draft inside it. An
    // assertion on the URL alone would pass even if the jump navigated anyway.
    expect(draftValue()).toBe('half-written prompt')
    expect(screen.queryByTestId('inbox-page')).toBeNull()
  })

  it('jumps to the inbox once the confirm is accepted', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await paneReady()
    typeDraft('half-written prompt')
    openBell()
    fireEvent.click(await screen.findByRole('button', { name: /^Open inbox$/ }))
    await waitFor(() => expect(screen.getByTestId('inbox-page')).toBeInTheDocument())
  })

  it('does not ask when the page has nothing at stake', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await paneReady()
    openBell()
    fireEvent.click(await screen.findByRole('button', { name: /^Open inbox$/ }))
    expect(confirmSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByTestId('inbox-page')).toBeInTheDocument())
  })

  it("keeps the page on screen when the detail panel's own jump is declined", async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await paneReady()
    typeDraft('half-written prompt')
    openBell()
    // The detail panel takes its own `navigate`, so wiring the bell's footer
    // says nothing about it: every button in here is a separate exit.
    fireEvent.click(await screen.findByText('Backup finished'))
    fireEvent.click(await screen.findByRole('button', { name: /^Open$/ }))
    expect(confirmSpy).toHaveBeenCalled()
    expect(draftValue()).toBe('half-written prompt')
    expect(screen.queryByTestId('schedule-page')).toBeNull()
  })

  it("follows the detail panel's jump once the confirm is accepted", async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await paneReady()
    typeDraft('half-written prompt')
    openBell()
    fireEvent.click(await screen.findByText('Backup finished'))
    fireEvent.click(await screen.findByRole('button', { name: /^Open$/ }))
    await waitFor(() => expect(screen.getByTestId('schedule-page')).toBeInTheDocument())
  })
  it('leaves the active slot alone when a slot jump is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    const store = createTestStore({
      notifications: { items: [CRON_NOTE], clearSeq: 0, ackSeq: 0, ackSeqByTs: {} },
    })
    renderDashboard(store)
    await paneReady()
    typeDraft('half-written prompt')
    openBell()
    fireEvent.click(await screen.findByText('Nightly backup finished'))
    fireEvent.click(await screen.findByRole('button', { name: /^Continue session$/ }))
    expect(confirmSpy).toHaveBeenCalled()
    // The draft survives — and so does the rest of the answer. This handler
    // switches the active slot BEFORE it navigates, so vetoing only the
    // navigation would keep the user on their draft while silently moving their
    // chat: the next visit to Chat would open a session they never chose.
    expect(draftValue()).toBe('half-written prompt')
    expect(store.getState().chat.activeSlot).not.toBe('slot-from-the-note')
  })
})
