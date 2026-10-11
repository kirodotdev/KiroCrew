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
import { screen, waitFor, fireEvent, cleanup, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { setActiveSlot } from '../store/chatSlice'
import { PREFILL_STORAGE_KEY } from '../utils/navIntent'
import App from '../App'
import SidePanelLayout, { useSidePanelLeaveGuard } from '../components/SidePanelLayout'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'

// Same isolation as the other App nav tests: stub the routed pages and the api
// client so App mounts without real network. The test only cares about the nav
// ROW, not about any page's content.
vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
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
        title="Customize"
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
// Capture the main-window nav-intent handler App registers, so a test can
// deliver the intent a popout would forward over the BroadcastChannel.
const navIntent = vi.hoisted(() => ({ handler: null as ((intent: { path: string }) => void) | null }))
vi.mock('../utils/artifactPopout', async (importOriginal) => {
  const real = await importOriginal<typeof import('../utils/artifactPopout')>()
  return {
    ...real,
    setNavIntentHandler: (fn: (intent: { path: string }) => void) => {
      navIntent.handler = fn
      return () => { if (navIntent.handler === fn) navIntent.handler = null }
    },
  }
})
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
    // The Customize row is `active` here: `active` is a PATHNAME match, and
    // the pathname is already /capabilities. But the row navigates to a bare
    // `/capabilities`, dropping `?tab=drafts` — and the pane is mounted on that
    // query, so the click unmounts it. Skipping the ask for any active row lost
    // the draft in silence; the ask has to be skipped only when the WHOLE
    // current URL already equals the row's target.
    fireEvent.click(navRow(/^Customize$/))
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
    fireEvent.click(navRow(/^Customize$/))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(draftValue()).toBe('half-written prompt')
  })
})

describe('popout-forwarded navigation leave guard', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  // A popout forwards its navigation to this window, which then replaces the
  // page on screen here. The popout cannot see this window's draft, so the
  // main window has to ask before it carries the intent out.
  it('keeps the page on screen when the confirm is declined', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)
    renderDashboard()
    await paneReady()
    typeDraft('half-written prompt')
    await waitFor(() => expect(navIntent.handler).not.toBeNull())
    act(() => { navIntent.handler!({ path: '/schedule' }) })
    expect(confirmSpy).toHaveBeenCalled()
    expect(draftValue()).toBe('half-written prompt')
  })

  it('carries the intent out once the confirm is accepted', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    renderDashboard()
    await paneReady()
    typeDraft('half-written prompt')
    await waitFor(() => expect(navIntent.handler).not.toBeNull())
    act(() => { navIntent.handler!({ path: '/schedule' }) })
    await waitFor(() => expect(screen.queryByLabelText('draft')).toBeNull())
  })
})

describe('popout hand-off to the session already on screen', () => {
  beforeEach(() => { localStorage.clear(); sessionStorage.clear() })
  afterEach(() => { vi.restoreAllMocks(); cleanup() })

  const deliver = (store: ReturnType<typeof createTestStore>, activeSlot: string) => {
    act(() => { store.dispatch(setActiveSlot(activeSlot)) })
    act(() => {
      navIntent.handler!({
        path: '/chat', slotKey: 'chat-1', prefill: { slotKey: 'chat-1', prompt: 'ref', append: 'ref' },
      } as Parameters<NonNullable<typeof navIntent.handler>>[0])
    })
  }

  // Staging on mainComposerAppend is what lets the split-view grid pane for
  // that slot pick it up as well as the single-session ChatPage.
  it('stages the append for the composer consumers when /chat shows the target slot', async () => {
    const store = createTestStore()
    renderWithProviders(<NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>, { route: '/chat', store })
    await waitFor(() => expect(navIntent.handler).not.toBeNull())
    deliver(store, 'chat-1')
    expect(store.getState().chat.mainComposerAppend).toEqual({ slot: 'chat-1', text: 'ref' })
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toBeNull()
  })

  it('seeds the prefill instead when /chat shows another slot', async () => {
    const store = createTestStore()
    renderWithProviders(<NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>, { route: '/chat', store })
    await waitFor(() => expect(navIntent.handler).not.toBeNull())
    deliver(store, 'chat-2')
    expect(store.getState().chat.mainComposerAppend).toBeNull()
    expect(sessionStorage.getItem(PREFILL_STORAGE_KEY)).toContain('"prompt":"ref"')
  })
})
