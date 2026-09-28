/**
 * Notification Center sheet — its crash fallback hands the crash to the agent.
 *
 * The sheet renders inside its own `ErrorBoundary` with a custom fallback (a
 * small panel over the bell, not the boundary's default card). The panel used
 * to offer only "Open the full inbox": a dead end, because the inbox page is not
 * where a render crash gets fixed. The fallback now mounts `AskAgentButton`
 * with the CAUGHT ERROR's message, which is the key the button uses to recover
 * the journaled report at click time — so the agent receives the crash
 * (message, `notifications-bell` scope, component stack), not the panel's
 * headline. The hand-off is SOFT and runs through the same leave gate as the
 * inbox link: a full page load would rebuild the store and drop every draft it
 * holds (a Remote Crew form under edit lives in `instances.crewForms` precisely
 * so an in-app navigation keeps it), while the soft route keeps the store and
 * lets the page on screen veto the jump.
 */
import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { screen, fireEvent, within, waitFor } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import { NavigationLeaveGuardProvider } from '../components/NavigationLeaveGuard'
import { EMPTY_INSTANCE_FORM } from '../pages/settings/InstanceFormFields'
import { consumeChatHandoff, sendErrorToChat } from '../utils/errorReport'

// The page beneath the sheet, standing in for one that registered a leave
// guard (a SidePanelLayout pane holding a component-local draft). Its answer
// is set per test; `asked` counts the gate's questions.
const guard = vi.hoisted(() => ({ answer: true, asked: 0 }))

vi.mock('../pages/ChatPage', () => ({ default: () => <div data-testid="chat-page">ChatPage</div> }))
vi.mock('../pages/SystemPage', () => ({ default: () => null }))
vi.mock('../pages/ProjectsPage', () => ({ default: () => null }))
vi.mock('../pages/LogsPage', async () => {
  const { useRegisterNavigationLeaveGuard } = await import('../components/NavigationLeaveGuard')
  function GuardedLogsPage() {
    useRegisterNavigationLeaveGuard(() => { guard.asked += 1; return guard.answer })
    return <div data-testid="logs-page">LogsPage</div>
  }
  return { default: GuardedLogsPage }
})
vi.mock('../pages/KiroCrewAgentsPage', () => ({ default: () => null }))
vi.mock('../pages/NotificationsPage', () => ({ default: () => null }))
vi.mock('../pages/SchedulePage', () => ({ default: () => null }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: vi.fn(() => ({ agents: [{ name: 'kirocrew' }], defaultAgent: 'kirocrew' })) }))
vi.mock('../providers/context', () => ({ useProvider: () => ({ id: 'acp' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span>, Lightbox: () => null }))

// The throwing child: the feed is the sheet's body, and it is rendered only once
// the bell opens the sheet, so the boundary engages on the click and nowhere
// else in the shell. Unconditional, not a counter — React re-invokes a throwing
// render to rebuild the component stack, and a "throw once" child would
// succeed on the retry. The error itself is chosen per test.
const crash = vi.hoisted(() => ({ make: (): Error => new Error('zzq-feed-broke') }))
vi.mock('../components/notifications/NotificationFeed', async importOriginal => {
  const mod = await importOriginal<typeof import('../components/notifications/NotificationFeed')>()
  return { ...mod, default: () => { throw crash.make() } }
})

// A call-through spy, not a stub: the prompt tests read its arguments, and the
// draft tests need the REAL route — a soft hand-off through the router
// navigator App installs versus a full page load — to be what runs.
vi.mock('../utils/errorReport', async importOriginal => {
  const mod = await importOriginal<typeof import('../utils/errorReport')>()
  return { ...mod, sendErrorToChat: vi.fn((...args: Parameters<typeof mod.sendErrorToChat>) => mod.sendErrorToChat(...args)) }
})

vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    notifications: vi.fn().mockResolvedValue({ notifications: [] }),
    status: vi.fn().mockResolvedValue({ uptime: '1h', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }),
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
    constructor(status: number, message: string) { super(message); this.status = status }
  },
}))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as unknown as typeof ResizeObserver

import App from '../App'

/** An unsaved Remote Crew form: the draft that lives in the store, not in a
 *  component, so that an in-app navigation keeps it — and a full page load
 *  does not. */
const CREW_DRAFT = { ...EMPTY_INSTANCE_FORM, name: 'zzq-half-filled crew' }

async function openCrashedSheet({ route = '/logs' }: { route?: string } = {}) {
  const store = createTestStore({
    notifications: { items: [], clearSeq: 0, ackSeq: 0, ackSeqByTs: {} },
    instances: { warm: {}, activeId: null, mru: [], unread: {}, ready: {}, host: null, crewForms: { add: CREW_DRAFT, edit: null } },
  })
  renderWithProviders(<NavigationLeaveGuardProvider><App /></NavigationLeaveGuardProvider>, { route, store })
  fireEvent.click(await screen.findByLabelText('Notifications'))
  const headline = await screen.findByText('Notifications failed to load')
  // The fallback panel: the material card the headline sits in.
  const panel = headline.closest('[data-nc-material]') as HTMLElement
  expect(panel, 'the throw must land in the sheet\'s own boundary, not the root one').not.toBeNull()
  return { panel, store }
}

const askTheAgent = (panel: HTMLElement) =>
  fireEvent.click(within(panel).getByRole('button', { name: 'Ask the agent' }))

describe('Notification Center sheet — the crash fallback hands off to the agent', () => {
  let consoleError: ReturnType<typeof vi.spyOn>
  // A full page load is the hand-off's LAST resort (no navigator installed, or
  // `hard`); happy-dom cannot perform one, so it is stubbed and counted.
  let fullLoad: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    vi.mocked(sendErrorToChat).mockClear()
    sessionStorage.clear()
    guard.answer = true
    guard.asked = 0
    // The boundary logs the caught throw on purpose; keep the run readable.
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    fullLoad = vi.spyOn(window.location, 'assign').mockImplementation(() => {})
  })
  afterEach(() => {
    consoleError.mockRestore()
    fullLoad.mockRestore()
    crash.make = () => new Error('zzq-feed-broke')
  })

  it('offers "Ask the agent" above the inbox link, and the hand-off carries the caught crash', async () => {
    const { panel } = await openCrashedSheet()

    // The inbox link the panel always had is still there.
    expect(within(panel).getByRole('button', { name: 'Open the full inbox' })).toBeInTheDocument()

    // The line under the button says what the press does, for a reader who
    // has never met the hand-off: it leaves this page, and the chat it opens
    // has the report pre-filled but NOT sent — the user still presses send.
    expect(within(panel).getByText(
      "Leaves this page and opens a new chat with this error's report already filled in, ready for you to send",
    )).toBeInTheDocument()
    askTheAgent(panel)

    expect(sendErrorToChat).toHaveBeenCalledTimes(1)
    const [prompt, opts] = vi.mocked(sendErrorToChat).mock.calls[0]
    // The boundary's OWN error reached the agent — resolved from the journal
    // (the scope rides as the report's code), not the panel's headline.
    expect(prompt).toContain('- Message: zzq-feed-broke')
    expect(prompt).toContain('- Code: notifications-bell')
    expect(prompt).not.toContain('Notifications failed to load')
    // Soft: the router carries the user to the chat with the store intact.
    expect(opts, 'the sheet\'s crash is contained to the sheet; a full load would drop every store-held draft').toEqual({ hard: false })
  })

  it('still hands off when the thrown error has no message: the name stands in for it', async () => {
    // `AskAgentButton` renders nothing with neither report nor message, so a
    // bare `throw new TypeError('')` would leave the panel with no hand-off at
    // all. The boundary journals `message || name` for the same reason; the
    // fallback must key its button on the same value or the lookup misses.
    crash.make = () => new TypeError('')
    const { panel } = await openCrashedSheet()

    askTheAgent(panel)

    expect(sendErrorToChat).toHaveBeenCalledTimes(1)
    const [prompt] = vi.mocked(sendErrorToChat).mock.calls[0]
    expect(prompt).toContain('- Message: TypeError')
    expect(prompt).toContain('- Code: notifications-bell')
  })

  it('keeps an unsaved Remote Crew form: the hand-off is an in-app navigation, never a full load', async () => {
    const { panel, store } = await openCrashedSheet()

    askTheAgent(panel)

    // The store outlives the jump — nothing rebuilt it.
    expect(fullLoad, 'a full page load rebuilds the store and loses the crew form under edit').not.toHaveBeenCalled()
    await screen.findByTestId('chat-page')
    expect(store.getState().instances.crewForms.add).toEqual(CREW_DRAFT)
    // The prompt is staged for the chat to drain into a fresh composer.
    expect(consumeChatHandoff()).toContain('- Message: zzq-feed-broke')
    // The sheet went with the page: its crash panel is leaving, not sitting
    // over the chat.
    await waitFor(() => expect(panel).toHaveAttribute('aria-hidden', 'true'))
  })

  it('asks the page on screen first, and a veto leaves everything exactly as it was', async () => {
    guard.answer = false
    const { panel, store } = await openCrashedSheet()

    askTheAgent(panel)

    expect(guard.asked, 'the hand-off must run through the same leave gate as the inbox link').toBe(1)
    // Vetoed BEFORE anything was staged: no prompt in the channel, no
    // navigation of either kind, the page and its draft untouched, and the
    // crash panel still up with its actions.
    expect(sendErrorToChat).not.toHaveBeenCalled()
    expect(consumeChatHandoff()).toBeNull()
    expect(fullLoad).not.toHaveBeenCalled()
    expect(screen.queryByTestId('chat-page')).toBeNull()
    expect(screen.getByTestId('logs-page')).toBeInTheDocument()
    expect(store.getState().instances.crewForms.add).toEqual(CREW_DRAFT)
    expect(panel).not.toHaveAttribute('aria-hidden')
    expect(within(panel).getByRole('button', { name: 'Ask the agent' })).toBeInTheDocument()
  })

  it('raised on the chat itself, it dismisses the sheet instead of leaving the crash panel over the composer it filled', async () => {
    // No route change happens here, so nothing else closes the sheet: the
    // hand-off has to.
    const { panel } = await openCrashedSheet({ route: '/chat' })

    askTheAgent(panel)

    expect(sendErrorToChat).toHaveBeenCalledTimes(1)
    expect(fullLoad).not.toHaveBeenCalled()
    await waitFor(() => expect(panel, 'the sheet must dismiss once the hand-off proceeded').toHaveAttribute('aria-hidden', 'true'))
  })
})
