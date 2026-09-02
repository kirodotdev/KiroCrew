/**
 * Regression test: the global sidebar asks the page on screen before it
 * navigates away from it.
 *
 * This is the exit that discarded a prompt draft in silence. The pane's own
 * guard covers the exits SidePanelLayout owns and `beforeunload` covers a real
 * document unload; a sidebar click is neither — it swaps the whole page without
 * unloading the document, so the only thing that can defend the draft is the row
 * itself asking first.
 *
 * Pinned against the REAL NavItem rather than a stand-in, because the whole
 * defect was that this specific row did not ask. NavigationLeaveGuard.test.tsx
 * pins the other half of the chain (a pane's guard reaching the channel through
 * SidePanelLayout); this pins that the row consults the channel, and that it
 * stays quiet for a row that navigates nowhere.
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, cleanup } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import App from '../App'
import SidePanelLayout, { useSidePanelLeaveGuard } from '../components/SidePanelLayout'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'

// Same isolation as the other App nav tests: stub the routed pages and the api
// client so App mounts without real network. The test only cares about the nav
// ROW, not about any page's content.
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/AgentsPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', () => ({ default: () => null }))
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
// The page at risk, with the REAL SidePanelLayout inside it: the pane is mounted
// conditionally on `?tab=`, which is what makes a dropped query string an
// unmount rather than a no-op. The draft lives in component-local state, as
// PromptsTab's does.
vi.mock('../pages/CapabilitiesPage', () => {
  function DraftPane() {
    const [draft, setDraft] = React.useState('')
    useSidePanelLeaveGuard(() => !draft || confirm('Discard unsaved changes?'))
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

/** Provider on the outside, as main.tsx mounts it around the router. */
const renderDashboard = (route = '/capabilities?tab=drafts') =>
  renderWithProviders(
    <NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>,
    { route },
  )

const navRow = (name: RegExp) => screen.getByRole('button', { name })
const typeDraft = (value: string) =>
  fireEvent.change(screen.getByLabelText('draft'), { target: { value } })
const draftValue = () => (screen.getByLabelText('draft') as HTMLInputElement).value
const paneReady = () => waitFor(() => expect(screen.getByLabelText('draft')).toBeInTheDocument())

describe('sidebar navigation leave guard', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  it('does not ask when the page has nothing at stake', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await paneReady()
    fireEvent.click(navRow(/^Schedule$/))
    expect(confirmSpy).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByLabelText('draft')).toBeNull())
  })

  it('keeps the page on screen when the confirm is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await paneReady()
    typeDraft('half-written prompt')
    fireEvent.click(navRow(/^Schedule$/))
    expect(confirmSpy).toHaveBeenCalled()
    // The pane is still mounted, which is what saves the draft inside it. An
    // assertion on the URL alone would pass even if the row navigated anyway.
    expect(draftValue()).toBe('half-written prompt')
  })

  it('navigates once the confirm is accepted', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await paneReady()
    typeDraft('half-written prompt')
    fireEvent.click(navRow(/^Schedule$/))
    await waitFor(() => expect(screen.queryByLabelText('draft')).toBeNull())
  })

  it('asks when the ACTIVE row would drop the query that keeps the pane mounted', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard('/capabilities?tab=drafts')
    await paneReady()
    typeDraft('half-written prompt')
    // The Capabilities row is `active` here: `active` is a PATHNAME match, and
    // the pathname is already /capabilities. But the row navigates to a bare
    // `/capabilities`, dropping `?tab=drafts` — and the pane is mounted on that
    // query, so the click unmounts it. Skipping the ask for any active row lost
    // the draft in silence; the ask has to be skipped only when the WHOLE
    // current URL already equals the row's target.
    fireEvent.click(navRow(/^Agent Capabilities$/))
    expect(confirmSpy).toHaveBeenCalled()
    expect(draftValue()).toBe('half-written prompt')
  })

  it('never asks for a row whose whole URL is already where we are', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard('/capabilities')
    await paneReady()
    typeDraft('half-written prompt')
    // No query to drop, so this click navigates to exactly where we already
    // are and unmounts nothing. A confirm the user did not earn is what teaches
    // them to click through the one that matters.
    fireEvent.click(navRow(/^Agent Capabilities$/))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(draftValue()).toBe('half-written prompt')
  })
})
