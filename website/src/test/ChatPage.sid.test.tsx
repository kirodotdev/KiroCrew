/**
 * Tests for persistent ?sid= URL parameter and slug behavior.
 *
 * Renders the REAL ChatPage with module-level mocks for child components.
 * Verifies: URL sync, session activation from URL, error handling,
 * slug generation, and backward compatibility with ?slot=.
 */
import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Routes, Route, useLocation, useSearchParams, useNavigate } from 'react-router-dom'
import { createTestStore } from './helpers'
import { deleteSlot, switchSlot } from '../store/chatSlice'
import { addSlotOptimistic, fetchSlots, sseConnected, sseSlotPatch, sseSlots } from '../store/dashboardSlice'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import type { ChatSlot } from '../types'

/** Deep-partial preloaded state for createTestStore — test fixtures intentionally
 *  omit fields the reducer fills from initialState. */
type PreloadState = {
  dashboard?: Partial<RootState['dashboard']>
  chat?: Partial<RootState['chat']>
}

// --- Stub child components (same as ChatPage.persist test) ---
vi.mock('react-virtuoso', () => ({ Virtuoso: () => null }))
vi.mock('../components/ChatInput', () => ({ default: () => null }))
vi.mock('../components/WelcomeView', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownRenderer', () => ({ default: () => null }))
vi.mock('../components/TypewriterText', () => ({ default: () => null }))
vi.mock('../components/OverlayDrawer', () => ({ default: () => null }))
vi.mock('../components/AgentDropdownList', () => ({ default: () => null }))
vi.mock('../components/ModelDropdownList', () => ({ default: () => null }))
vi.mock('../components/InfoTip', () => ({ default: () => null }))
vi.mock('../components/SegmentedControl', () => ({ default: () => null }))
vi.mock('../pages/chat/CollapsibleToolGroup', () => ({ default: () => null }))
vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../pages/chat/SessionColorPicker', () => ({ default: () => null }))
vi.mock('../pages/chat', () => ({ ChatFooter: () => null, AssistantMessage: () => null, McpInfoButton: () => null, UserMessage: () => null, CronAckBar: () => null, NotificationItem: () => null, PinnedPrompt: () => null }))
vi.mock('../pages/ChatSidebar', () => ({ default: () => null, SIDEBAR_MIN: 200, SIDEBAR_MAX: 500 }))
vi.mock('../pages/chat/ChatSettings', () => ({ loadChatConfig: () => ({ contentWidth: 'compact' }), CONTENT_WIDTH: { compact: { messages: '900px', input: '916px' }, comfortable: { messages: '84%', input: '85%' }, full: { messages: '92%', input: '93%' } } }))

// --- Stub hooks ---
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

// --- Stub API ---
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle', 'kirocrewConfig', 'agentResolvedModel'].map(k => [k, vi.fn().mockResolvedValue(
      k === 'chatSlotDetail' ? { messages: [], has_more: false, total: 0 } : {}
    )])
  ),
}))

// --- Browser APIs ---
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((q: string) => ({
    matches: false, media: q, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })),
})
globalThis.fetch = vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({}) }) as unknown as typeof fetch

import { closeHoldForUrl } from '../pages/chat/useChatPageSessionController'
import ChatPage from '../pages/ChatPage'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'
import { __resetErrorJournalForTests, recordError } from '../utils/errorReport'

/** Slot keys `chatSlotDetail` was asked for — i.e. which sessions got fetched. */
function detailCalls(): string[] {
  return vi.mocked(api.chatSlotDetail).mock.calls.map(c => c[0] as string)
}

const slot = (key: string, title?: string, mode = ''): ChatSlot => ({
  key, title: title ?? key, messages: 0, running: false, mode, created: '', last_ts: '',
})

/** Helper to capture the current URL from MemoryRouter */
let currentUrl = ''
let seenUrls: string[] = []
function UrlCapture() {
  const loc = useLocation()
  const [sp] = useSearchParams()
  currentUrl = loc.pathname + (sp.toString() ? '?' + sp.toString() : '')
  if (seenUrls.at(-1) !== currentUrl) seenUrls.push(currentUrl)
  return null
}

/** Exposes the router's navigate() so tests can drive a real Back/Forward POP. */
let navBack: () => void = () => {}
let navForward: () => void = () => {}
let navTo: (to: string) => void = () => {}
function NavController() {
  const n = useNavigate()
  navBack = () => n(-1)
  navForward = () => n(1)
  navTo = (to: string) => n(to)
  return null
}

function renderChatPage(opts: {
  route?: string
  /** Full history stack; the last entry is where the app starts. Overrides `route`. */
  entries?: string[]
  mode?: string
  activeSlot?: string | null
  slots?: ChatSlot[]
  /** Render the companion-panel variant on a HOST route (see the noUrlSync suite). */
  hostEmbed?: { noUrlSync?: boolean }
  /** Transcript already in the store, plus the slot its paging cursor describes. */
  messages?: RootState['chat']['messages']
  slotCursorKey?: string | null
  /** Has the slot list arrived? Defaults to `slots.length > 0`, the invariant a
   *  real boot holds: the flag is set by the writer that delivers the list. Pass
   *  false with a populated list only to model the pre-arrival window. */
  slotsLoaded?: boolean
}) {
  const { route = '/chat', entries, mode, activeSlot = null, slots = [], hostEmbed,
          messages = [], slotCursorKey = null, slotsLoaded = slots.length > 0 } = opts
  const preload: PreloadState = {
    dashboard: {
      status: { platform: 'darwin' }, connected: true, slots, slotsLoaded, approvalMode: 'normal',
      channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
      subagentRunning: {}, subagentDetails: {}, subagentText: {},
      sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
    },
    chat: {
      activeSlot, messages, slotCursorKey, slotRunning: false, slotStopping: false, slotState: 'idle',
      slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
      lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
      pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
      subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory: [],
      slotMessages: {}, slotLoading: false,
    },
  }
  const store = createTestStore(preload as Partial<RootState>)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const result = render(
    <QueryClientProvider client={qc}>
      <Provider store={store}>
        <ThemeProvider>
          <MemoryRouter initialEntries={entries ?? [route]}>
            <Routes>
              <Route path="/chat/:slug?" element={<ChatPage mode={mode} />} />
              {/* Stands in for any non-chat dashboard page a session link is
                  followed FROM (System, Telemetry) — it only has to be a
                  distinct history entry. */}
              <Route path="/developer" element={<div>developer</div>} />
              <Route
                path="/artifacts/:slug"
                element={<ChatPage embedded embedMode="chat" noUrlSync={hostEmbed?.noUrlSync} />}
              />
            </Routes>
            <UrlCapture />
            <NavController />
          </MemoryRouter>
        </ThemeProvider>
      </Provider>
    </QueryClientProvider>,
  )
  return { store, ...result }
}

beforeEach(() => {
  localStorage.clear()
  __resetErrorJournalForTests()
  currentUrl = ''
  seenUrls = []
})

afterEach(() => {
  // Restore real timers here, not only in the tests that install fakes: a fake
  // clock left armed by a failing assertion makes every later test in the file
  // time out, which reads as a cascade of unrelated breakage.
  vi.useRealTimers()
  vi.clearAllMocks()
})

const slots = [
  slot('chat-1-100', 'Debug video playback'),
  slot('chat-2-200', 'Fix login bug'),
  slot('chat-3-300'), // no title (title === key)
]

/** Legacy Autopilot slots: still persisted under the retired 'orchestrator'
 *  mode, rendered as ordinary chats. */
const legacyAutopilotSlots = [
  slot('orch-1-100', 'Plan migration', 'orchestrator'),
  slot('orch-2-200', 'Review design', 'orchestrator'),
]

/** A ?msg= deep link must survive the slot switch it arrives with. The effect
 *  reads `state.chat.messages`, which still holds the OUTGOING chat until the
 *  switch settles — so without a slot-identity gate the target is "not found"
 *  in the wrong transcript, the one-shot ref is spent, and the jump is lost. */
describe('ChatPage ?sid= + ?msg= deep link across a slot switch', () => {
  const slots: ChatSlot[] = [
    { key: 'chat-1-100', title: 'short chat', agent: 'a', mode: 'chat' } as ChatSlot,
    { key: 'chat-2-200', title: 'long chat', agent: 'a', mode: 'chat' } as ChatSlot,
  ]
  /** A complete window for the chat being LEFT. The deep-link target belongs to
   *  the requested chat, so it is legitimately absent from this array. */
  const outgoing = [
    { role: 'user', content: 'a', ts: '2026-01-01T00:00:00Z' },
    { role: 'assistant', content: 'b', ts: '2026-01-01T00:00:01Z' },
  ] as RootState['chat']['messages']
  const DEEP_LINK = '/chat?sid=chat-2-200&msg=2025-06-01T00%3A00%3A00Z'

  it('does not declare the target unavailable while the requested chat is still activating', async () => {
    renderChatPage({ route: DEEP_LINK, activeSlot: 'chat-1-100', slots, messages: outgoing, slotCursorKey: 'chat-1-100' })
    // The outgoing window is complete, so an ungated hand-off hits the dead-end
    // branch and paints a false notice against a chat the link never named.
    await new Promise(r => setTimeout(r, 250))
    // Matched on "no longer", which BOTH unavailability notices still share: a
    // matcher tied to wording only one of them carries would pass vacuously here.
    expect(screen.queryByText(/no longer/i)).toBeNull()
  })

  it('acts on the deep link once the window belongs to the requested chat (control)', async () => {
    // Target absent from a window whose extent is known, so the hand-off is
    // correct to make here and the gate must not suppress it.
    renderChatPage({ route: DEEP_LINK, activeSlot: 'chat-2-200', slots, messages: outgoing, slotCursorKey: 'chat-2-200' })
    const notice = await screen.findByText(/no longer/i)
    // Both strings share "no longer", so the pin word is what discriminates:
    // this reader followed a link and may never have pinned anything.
    expect(notice.textContent).not.toMatch(/pinned/i)
  })

  /** A same-tick twin: identical `ts`, different `mid`. The helper falls back to ts
   *  when the requested mid is absent, which on a bounded page is a DIFFERENT row. */
  const SAME_TICK = [
    { role: 'user', content: 'a', ts: '2026-01-01T00:00:00Z' },
    { role: 'assistant', content: 'twin', ts: '2025-06-01T00:00:00Z', meta: { mid: 'mid-other' } },
  ] as RootState['chat']['messages']

  it('hands off when the requested mid is off-page, rather than taking a same-ts twin', async () => {
    // Accepting the twin highlights the wrong message with no signal at all, which is
    // strictly worse than paging: the mid exists to discriminate exactly this pair.
    renderChatPage({ route: `${DEEP_LINK}&mid=mid-offpage`, activeSlot: 'chat-2-200', slots, messages: SAME_TICK, slotCursorKey: 'chat-2-200' })
    expect(await screen.findByText(/no longer/i)).toBeTruthy()
  })

  it('still resolves a legacy link carrying NO mid, by ts alone', async () => {
    // Opposite direction: the ts fallback is what the helper documents for older links,
    // so a guard that also rejected THEM would be worse than the defect it fixes.
    renderChatPage({ route: DEEP_LINK, activeSlot: 'chat-2-200', slots, messages: SAME_TICK, slotCursorKey: 'chat-2-200' })
    await new Promise(r => setTimeout(r, 250))
    expect(screen.queryByText(/no longer/i)).toBeNull()
  })
})

/** A transient paging error must not be reported with permanent-deletion copy. The
 *  `earlier` origin already had a retry string; the `link` origin this PR introduces
 *  fell through to the not-found writer, so a network blip told a reader following a
 *  live link that the message was gone. Asserted on source text because the routing
 *  ternary is shared with the pin path, whose own suite pins it the same way.
 */
describe('paging-failure notice by jump origin', () => {
  const GONE = /no longer/i
  const src = readFileSync(resolve(__dirname, '../pages/ChatPage.tsx'), 'utf8')

  it('routes the link origin to the retry copy, not to the not-found writer', () => {
    expect(src).toContain("pendingPinnedJump.origin === 'earlier' || pendingPinnedJump.origin === 'link'")
    // Positive control for the matcher: the retry string it selects is real copy.
    expect(i18nT('components.chatPane.earlier_messages_load_failed')).toMatch(/try again/i)
    expect(i18nT('components.chatPane.earlier_messages_load_failed')).not.toMatch(GONE)
  })

  it('keeps the not-found copy permanent-phrased, so the pair stays distinguishable', () => {
    // Negative control: satisfying the test above by making the NOT-FOUND copy
    // retryable would be a different regression, so it must still read permanent.
    expect(i18nT('pages.chat.deepLink.message_unavailable')).toMatch(GONE)
    expect(i18nT('pages.chat.pins.message_unavailable')).toMatch(GONE)
  })
})

describe('ChatPage ?sid= URL parameter', () => {
  describe('URL sync on active slot', () => {
    it('writes ?sid= to URL when activeSlot is set', async () => {
      renderChatPage({ activeSlot: 'chat-1-100', slots })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
    })

    it('includes slug from session title', async () => {
      renderChatPage({ activeSlot: 'chat-1-100', slots })
      await waitFor(() => {
        expect(currentUrl).toContain('/chat/debug-video-playback')
        expect(currentUrl).toContain('sid=chat-1-100')
      })
    })

    it('omits slug when title equals key', async () => {
      renderChatPage({ activeSlot: 'chat-3-300', slots })
      await waitFor(() => {
        expect(currentUrl).toMatch(/^\/chat\?sid=chat-3-300$/)
      })
    })
  })

  // ── noUrlSync (artifact companion panel) ─────────────────────────────────
  // noUrlSync must disable BOTH directions of the URL<->session sync. Gating
  // only the WRITE side is not enough: the host route is not guaranteed to be
  // sid-free, and an ungated READ effect would switch the embedded panel onto
  // whatever session ?sid= names — so the user would type into the artifact
  // panel and the message would land in an unrelated conversation.
  describe('noUrlSync on a host route', () => {
    it('ignores ?sid= on the host route', async () => {
      const { store } = renderChatPage({
        route: '/artifacts/cr-queue?sid=chat-2-200',
        slots,
        hostEmbed: { noUrlSync: true },
      })
      // Give the mount-activation effect every chance to fire.
      await act(async () => { await new Promise(r => setTimeout(r, 150)) })
      expect(store.getState().chat.activeSlot).not.toBe('chat-2-200')
    })

    it('honors ?sid= on the same route without noUrlSync (control)', async () => {
      // Proves the assertion above can actually observe a switch — otherwise it
      // would pass even if the read effects never ran for an unrelated reason.
      const { store } = renderChatPage({
        route: '/artifacts/cr-queue?sid=chat-2-200',
        slots,
        hostEmbed: { noUrlSync: false },
      })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    })

    it('never rewrites the host URL', async () => {
      renderChatPage({
        route: '/artifacts/cr-queue',
        slots,
        activeSlot: 'chat-1-100',
        hostEmbed: { noUrlSync: true },
      })
      await act(async () => { await new Promise(r => setTimeout(r, 150)) })
      expect(currentUrl).toBe('/artifacts/cr-queue')
    })
  })

  describe('session activation from URL', () => {
    it('activates session matching ?sid= on load', async () => {
      const { store } = renderChatPage({ route: '/chat?sid=chat-2-200', slots })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    })

    it('activates session from legacy ?slot= param', async () => {
      const { store } = renderChatPage({ route: '/chat?slot=chat-2-200', slots })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    })

    it('shows error for invalid ?sid=', async () => {
      vi.useFakeTimers()
      renderChatPage({ route: '/chat?sid=nonexistent', slots })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.getByText(/session "nonexistent" not found/i)).toBeTruthy()
      vi.useRealTimers()
    })

    /** The timer measures "the list lacks this key", so it must not start before
     *  the list exists. Firing is one-way — it clears `initialSidRef` — so a list
     *  arriving after the deadline finds nothing left to resolve, and the banner
     *  denies a session that is live. Only a reopen recovers, which is what makes
     *  this read to the user as an intermittent "not found" on a good link. */
    it('does not declare a session missing while the slot list has not arrived', async () => {
      vi.useFakeTimers()
      const { store } = renderChatPage({ route: '/chat?sid=chat-9-900', slots: [], slotsLoaded: false })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.queryByText(/session "chat-9-900" not found/i)).toBeNull()

      await act(async () => { store.dispatch(sseSlots([slot('chat-9-900', 'Late Session')])) })
      await vi.advanceTimersByTimeAsync(50)
      expect(store.getState().chat.activeSlot).toBe('chat-9-900')
      vi.useRealTimers()
    })

    /** A slot list can be authoritative for what it carries without being the
     *  final restored list. If a later frame proves the linked session is live,
     *  the deadline's stale verdict must be withdrawn and the link resolved. */
    it('withdraws not found when a later slot frame carries the key', async () => {
      vi.useFakeTimers()
      const { store } = renderChatPage({ route: '/chat?sid=chat-9-900', slots: [], slotsLoaded: false })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.queryByText(/session "chat-9-900" not found/i)).toBeNull()

      await act(async () => { store.dispatch(sseSlots(slots)) })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.getByText(/session "chat-9-900" not found/i)).toBeTruthy()

      await act(async () => {
        store.dispatch(sseSlots([...slots, slot('chat-9-900', 'Late Session')]))
      })
      await vi.advanceTimersByTimeAsync(50)
      expect(screen.queryByText(/session "chat-9-900" not found/i)).toBeNull()
      expect(store.getState().chat.activeSlot).toBe('chat-9-900')
      vi.useRealTimers()
    })

    /** A late list entry proves the key exists, but its transcript can still fail
     *  to load. Recovery must replace the stale not-found verdict with the
     *  existing open-session failure instead of clearing every visible error. */
    it('keeps a visible error when the late session detail cannot load', async () => {
      vi.useFakeTimers()
      const report = recordError({
        source: 'api',
        message: 'detail load failed',
        status: 503,
        code: 'slot_detail_failed',
        endpoint: '/api/chat/slots/chat-9-900',
      })
      vi.mocked(api.chatSlotDetail).mockRejectedValueOnce(new Error(report.message))
      const { store } = renderChatPage({ route: '/chat?sid=chat-9-900', slots: [], slotsLoaded: false })
      await act(async () => { store.dispatch(sseSlots(slots)) })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.getByText(/session "chat-9-900" not found/i)).toBeTruthy()

      await act(async () => {
        store.dispatch(sseSlots([...slots, slot('chat-9-900', 'Late Session')]))
        await Promise.resolve()
        await Promise.resolve()
      })
      expect(screen.queryByText(/session "chat-9-900" not found/i)).toBeNull()
      expect(screen.getByText(
        i18nT('store.chatSlice.session_open_error_named', { name: 'Late Session' }),
      )).toBeTruthy()
      expect(screen.getAllByRole('alert')).toHaveLength(1)
      expect(store.getState().chat.switchSlotGone?.report).toMatchObject({
        endpoint: '/api/chat/slots/chat-9-900',
        status: 503,
        code: 'slot_detail_failed',
      })
      vi.useRealTimers()
    })

    /** Control for the recovery above: repeated authoritative frames that omit
     *  the key do not revoke the missing-session verdict. */
    it('keeps not found while later slot frames still omit the key', async () => {
      vi.useFakeTimers()
      const { store } = renderChatPage({ route: '/chat?sid=chat-9-900', slots: [], slotsLoaded: false })
      await act(async () => { store.dispatch(sseSlots(slots)) })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.getByText(/session "chat-9-900" not found/i)).toBeTruthy()

      await act(async () => { store.dispatch(sseSlots([...slots])) })
      await vi.advanceTimersByTimeAsync(50)
      expect(screen.getByText(/session "chat-9-900" not found/i)).toBeTruthy()
      expect(store.getState().chat.activeSlot).not.toBe('chat-9-900')
      vi.useRealTimers()
    })
  })

  // Regression: loading on a chat URL (?sid= present) must not freeze switching.
  // If pendingSidRef were overloaded for both deep-link activation AND a POP in
  // flight, the deep-link load would trip the POP bail so the first switch never
  // updated the URL until a reload; loading at /chat (no ?sid) hides that.
  describe('switch after deep-link load (Mesh chat-switch bug)', () => {

    /** Revoking the stale verdict must not undo a session choice made after the
     *  deadline. The late frame clears the lie but leaves the user where they went. */
    it('does not override a user switch when the denied slot arrives later', async () => {
      vi.useFakeTimers()
      const { store } = renderChatPage({
        route: '/chat?sid=chat-9-900',
        activeSlot: 'chat-1-100',
        slots: [],
        slotsLoaded: false,
      })
      await act(async () => { store.dispatch(sseSlots(slots)) })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.getByText(/session "chat-9-900" not found/i)).toBeTruthy()

      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await act(async () => {
        store.dispatch(sseSlots([...slots, slot('chat-9-900', 'Late Session')]))
      })
      await vi.advanceTimersByTimeAsync(50)
      expect(screen.queryByText(/session "chat-9-900" not found/i)).toBeNull()
      expect(store.getState().chat.activeSlot).toBe('chat-2-200')
      vi.useRealTimers()
    })

    it('does not override a user who switches away and back before the denied slot arrives', async () => {
      vi.useFakeTimers()
      const { store } = renderChatPage({
        route: '/chat?sid=chat-9-900',
        activeSlot: 'chat-1-100',
        slots: [],
        slotsLoaded: false,
      })
      await act(async () => { store.dispatch(sseSlots(slots)) })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.getByText(/session "chat-9-900" not found/i)).toBeTruthy()

      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await act(async () => { await store.dispatch(switchSlot('chat-1-100')) })
      await act(async () => {
        store.dispatch(sseSlots([...slots, slot('chat-9-900', 'Late Session')]))
      })
      await vi.advanceTimersByTimeAsync(50)
      expect(screen.queryByText(/session "chat-9-900" not found/i)).toBeNull()
      expect(store.getState().chat.activeSlot).toBe('chat-1-100')
      vi.useRealTimers()
    })

    it('updates URL when switching sessions after loading with ?sid= present', async () => {
      const { store } = renderChatPage({ route: '/chat/fix-login-bug?sid=chat-2-200', slots })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))

      await act(async () => { await store.dispatch(switchSlot('chat-1-100')) })

      // URL must follow the switch.
      await waitFor(() => {
        expect(currentUrl).toContain('sid=chat-1-100')
        expect(currentUrl).toContain('/chat/debug-video-playback')
      })
      expect(currentUrl).not.toContain('sid=chat-2-200')
    })
  })

  // Regression: a deep link followed from ANOTHER dashboard page (System's
  // "Session & Task Memory" rows, Telemetry's conversation links) mounts ChatPage
  // with a Redux `activeSlot` already carried over from earlier in the visit.
  describe('deep link from another page (activeSlot already set)', () => {
    it('activates the session named by ?sid= instead of the carried-over slot', async () => {
      const { store } = renderChatPage({
        route: '/chat?sid=chat-2-200',
        activeSlot: 'chat-1-100',
        slots,
      })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      // The carried-over slot must not be re-fetched: that switchSlot is what
      // used to land the user back in the session they came from.
      expect(detailCalls()).not.toContain('chat-1-100')
    })

    // The switch must not leave a history entry for the slot it switched AWAY
    // from: the URL-sync effect runs later in the same commit with the
    // pre-switch activeSlot, and a PUSH there means Back opens that session
    // instead of returning to the page the link was clicked on.
    it('leaves Back pointing at the page the link came from', async () => {
      const { store } = renderChatPage({
        entries: ['/developer', '/chat?sid=chat-2-200'],
        activeSlot: 'chat-1-100',
        slots,
      })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toBe('/developer'))
    })

    // Legacy `?slot=` resolves through the same path, so it must release the
    // in-flight flag too — otherwise URL sync stays wedged for the whole mount
    // and a later switch leaves the URL (and a reload) on the wrong session.
    it('normalizes a legacy ?slot= deep link and keeps URL sync alive', async () => {
      const { store } = renderChatPage({
        entries: ['/developer', '/chat?slot=chat-2-200'],
        activeSlot: 'chat-1-100',
        slots,
      })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      expect(currentUrl).not.toContain('slot=')
      // A switch AFTER the deep link must still reach the URL.
      await act(async () => { await store.dispatch(switchSlot('chat-1-100')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
    })

    // The same legacy parameter naming the session that is ALREADY active, on a
    // session whose title equals its key (so the path needs no slug): the sync
    // effect's no-op bail must not keep `?slot=` in the address bar.
    it('normalizes a legacy ?slot= URL that names the active session', async () => {
      const { store } = renderChatPage({
        entries: ['/developer', '/chat?slot=chat-1-100'],
        activeSlot: 'chat-1-100',
        slots: [slot('chat-1-100'), slot('chat-2-200')],
      })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
      expect(currentUrl).not.toContain('slot=')
    })

    // A session created and linked in one go (the app pages' create-then-navigate)
    // reaches this URL before its slots frame does. The wait must not leak a
    // history entry for the carried-over session.
    it('waits for a slot that arrives later without polluting history', async () => {
      const { store } = renderChatPage({
        entries: ['/developer', '/chat?sid=chat-9-900'],
        activeSlot: 'chat-1-100',
        slots,
      })
      // The link cannot resolve yet — the slot does not exist in the list.
      await act(async () => { await new Promise(r => setTimeout(r, 150)) })
      expect(store.getState().chat.activeSlot).toBe('chat-1-100')

      await act(async () => {
        store.dispatch(sseSlots([...slots, slot('chat-9-900', 'Late Session')]))
      })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-9-900'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-9-900'))

      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toBe('/developer'))
    })

    // Abandoning a pending link must not leave URL sync wedged: the user can pick
    // another session while the linked slot is still missing, and the not-found
    // timeout cannot help — clearing `initialSidRef` on that path is exactly what
    // stops the timeout from firing.
    it('keeps URL sync alive when the user switches away from a pending deep link', async () => {
      const { store } = renderChatPage({
        entries: ['/developer', '/chat?sid=chat-9-900'],
        activeSlot: 'chat-1-100',
        slots,
      })
      await act(async () => { await new Promise(r => setTimeout(r, 150)) })
      expect(store.getState().chat.activeSlot).toBe('chat-1-100')

      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
    })

    // The skip above is scoped to a pending deep link only. Plain nav-away-and-back
    // (no ?sid=) must still re-fetch, or a session reopened from the sidebar shows
    // whatever messages Redux happened to be holding.
    it('still re-fetches the carried-over slot when no ?sid= is present', async () => {
      renderChatPage({ route: '/chat', activeSlot: 'chat-1-100', slots })
      await waitFor(() => expect(detailCalls()).toContain('chat-1-100'))
    })

    // The not-found timeout must NOT fetch the session on screen. Five seconds is
    // long enough for the user to type and send; a refresh landing after that
    // optimistic row would replace it (and `running`) with a server snapshot that
    // predates the turn, so the message they just sent would vanish. Staleness is
    // the lesser fault, and the banner explains the failed link.
    it('does not fetch the on-screen session when the deep link is declared not found', async () => {
      vi.useFakeTimers()
      renderChatPage({ route: '/chat?sid=nonexistent', activeSlot: 'chat-1-100', slots })
      await vi.advanceTimersByTimeAsync(5100)
      expect(detailCalls()).not.toContain('chat-1-100')
      expect(screen.getByText(/session "nonexistent" not found/i)).toBeTruthy()
      vi.useRealTimers()
    })
  })

  describe('URL wins over localStorage', () => {
    it('activates URL session even when localStorage has different value', async () => {
      localStorage.setItem('mc-active-slot-chat', 'chat-1-100')
      const { store } = renderChatPage({ route: '/chat?sid=chat-2-200', slots })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    })
  })

  describe('legacy Autopilot slots (unified view)', () => {
    it('uses /chat base path for a legacy Autopilot slot', async () => {
      const allSlots = [...slots, ...legacyAutopilotSlots]
      renderChatPage({ route: '/chat', activeSlot: 'orch-1-100', slots: allSlots })
      await waitFor(() => {
        expect(currentUrl).toContain('/chat/plan-migration')
        expect(currentUrl).toContain('sid=orch-1-100')
      })
    })

    it('keeps a legacy Autopilot slot under the unified /chat surface', async () => {
      const allSlots = [...slots, ...legacyAutopilotSlots]
      renderChatPage({ route: '/chat?sid=orch-1-100', slots: allSlots })
      await waitFor(() => {
        expect(currentUrl).toContain('/chat')
        expect(currentUrl).not.toMatch(/^\/orchestrated/)
      })
    })
  })

  describe('message deep-link (?msg=)', () => {
    it('cleans ?msg= from URL after consumption (one-shot)', async () => {
      renderChatPage({ route: '/chat?sid=chat-1-100&msg=2025-05-13T14:00:00.000Z', slots })
      await waitFor(() => {
        expect(currentUrl).toContain('sid=chat-1-100')
        expect(currentUrl).not.toContain('msg=')
      })
    })

    it('preserves ?sid= when ?msg= is cleaned', async () => {
      renderChatPage({ route: '/chat?sid=chat-1-100&msg=2025-05-13T14:00:00.000Z', slots })
      await waitFor(() => {
        expect(currentUrl).toContain('sid=chat-1-100')
      })
    })
  })

  describe('backward compatibility', () => {
    it('works without ?sid= param (falls back to localStorage)', async () => {
      localStorage.setItem('mc-active-slot-chat', 'chat-2-200')
      const { store } = renderChatPage({ route: '/chat', slots })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    })

    it('works without ?sid= and no localStorage (picks first slot)', async () => {
      const { store } = renderChatPage({ route: '/chat', slots })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
    })
  })

  describe('slug generation', () => {
    it('slugifies title to lowercase kebab-case', async () => {
      renderChatPage({ activeSlot: 'chat-1-100', slots })
      await waitFor(() => expect(currentUrl).toContain('/chat/debug-video-playback'))
    })

    it('strips special characters from slug', async () => {
      const specialSlots = [slot('chat-4-400', 'Fix: login & auth (v2)!')]
      renderChatPage({ activeSlot: 'chat-4-400', slots: specialSlots })
      await waitFor(() => expect(currentUrl).toContain('/chat/fix-login-auth-v2'))
    })

    it('truncates slug to 80 chars', async () => {
      const longTitle = 'a'.repeat(100)
      const longSlots = [slot('chat-5-500', longTitle)]
      renderChatPage({ activeSlot: 'chat-5-500', slots: longSlots })
      await waitFor(() => {
        const path = currentUrl.split('?')[0]
        // /chat/ = 6 chars, slug should be <= 80
        expect(path.length).toBeLessThanOrEqual(6 + 80)
      })
    })
  })

  // Regression: browser Back/Forward (history POP) must retrace sessions across
  // MULTIPLE steps. The hazard: on a POP, an activeSlot→?sid sync effect running
  // with a STALE activeSlot pushes a spurious entry, so a second goBack jumps to
  // the wrong session and goForward sticks. A NavController exposes the router's
  // navigate(); navigate(-1/+1) is a real POP (useNavigationType()==='POP').
  describe('browser Back/Forward (history POP) retrace', () => {
    function renderForPop(initialSlots: ChatSlot[]) {
      const preload: PreloadState = {
        dashboard: {
          // connected: true is required — the POP-handler effect bails on
          // `if (!connected) return` (so offline tabs don't dispatch a
          // switchSlot that would clear messages). These tests exercise
          // the POP retrace logic itself, which inherently needs the
          // gateway available.
          status: { platform: 'darwin' }, connected: true, slots: initialSlots, slotsLoaded: true, approvalMode: 'normal',
          channelTrusted: false, refreshTrigger: 0, unreadSlots: [], updateProgress: null,
          subagentRunning: {}, subagentDetails: {}, subagentText: {},
          sessionDefaultColor: null, sessionColorsMode: 'tint', sessionColorsPalette: 'horizon', sessionColorsIntensity: 'clear',
        },
        chat: {
          activeSlot: null, messages: [], slotRunning: false, slotStopping: false, slotState: 'idle',
          slotStatusDetail: {}, slotHasMore: false, slotOldestIndex: 0, loadingOlder: false,
          lastChunkSeq: undefined, history: [], historyHasMore: false, historyOffset: 0,
          pendingInput: null, slotContextPct: {}, voicePlaying: false, voiceAudio: null,
          subagents: {}, toolLog: [], activityOpen: false, activityTab: 'logs', slotActivity: {}, slotHistory: [],
          slotMessages: {}, slotLoading: false,
        },
      }
      const store = createTestStore(preload as Partial<RootState>)
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
      render(
        <QueryClientProvider client={qc}>
          <Provider store={store}>
            <ThemeProvider>
              <MemoryRouter initialEntries={['/chat']}>
                <Routes>
                  <Route path="/chat/:slug?" element={<ChatPage />} />
                </Routes>
                <UrlCapture />
                <NavController />
              </MemoryRouter>
            </ThemeProvider>
          </Provider>
        </QueryClientProvider>,
      )
      return { store }
    }

    async function settle(store: ReturnType<typeof createTestStore>, rows: ChatSlot[]) {
      // The first three authoritative lists spend and retire the confirmed close
      // hold. The retirement list is still filtered; only the fourth list can
      // authoritatively establish whether the key remains absent.
      for (let i = 0; i < 4; i++) {
        await act(async () => { store.dispatch(sseSlots(rows)) })
      }
    }

    it('retraces the correct session across two Back steps then Forward', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      // First slot auto-activates (no ?sid, no localStorage) → history entry A.
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))

      // Switch A→B→C: each genuine switch PUSHES a ?sid history entry.
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      // Back once: C → B.
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))

      // Back again: B → A (must not land on chat-3-300 via a spurious push).
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))

      // Forward: A → B (the forward stack must not be corrupted and stuck).
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    })

    it('does not rewrite a live history entry omitted by a stale slot list', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      vi.mocked(api.chatSlots).mockResolvedValueOnce([navSlots[0], navSlots[2]] as never)
      await act(async () => { await store.dispatch(fetchSlots()) })
      expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-2-200')

      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      await act(async () => { store.dispatch(sseSlots(navSlots)) })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).toContain('chat-2-200'))

      for (let i = 0; i < 2 && store.getState().chat.activeSlot !== 'chat-2-200'; i++) {
        await act(async () => { navBack() })
        await new Promise(r => setTimeout(r, 20))
      }
      expect(store.getState().chat.activeSlot).toBe('chat-2-200')
    })

    // A session closed here and then resumed under the same key is live again:
    // a later list that omits it must not get its history entry rewritten.
    it('does not rewrite the entry of a closed session that was resumed', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      await act(async () => { await store.dispatch(deleteSlot('chat-2-200')) })
      await act(async () => { store.dispatch(addSlotOptimistic(navSlots[1])) })
      vi.mocked(api.chatSlots).mockResolvedValueOnce([navSlots[0], navSlots[2]] as never)
      await act(async () => { await store.dispatch(fetchSlots()) })
      expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-2-200')
      await settle(store, navSlots)
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).toContain('chat-2-200'))

      await act(async () => { navBack() })
      for (let i = 0; i < 2 && store.getState().chat.activeSlot !== 'chat-2-200'; i++) {
        await act(async () => { navBack() })
        await new Promise(r => setTimeout(r, 20))
      }
      expect(store.getState().chat.activeSlot).toBe('chat-2-200')
    })

    // Regression (URL-lock): after a Back/Forward POP, useNavigationType() stays
    // 'POP' until our own navigate() runs. A subsequent sidebar switch changes
    // activeSlot, re-firing the POP→sid effect while still 'POP'; reading the
    // stale URL sid there would revert the switch and lock the URL to one chat.
    // location.key gating must let the switch stick.
    it('allows switching to a different session after a Back navigation', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))

      // Back: B → A (a real POP, navigationType now sticks at 'POP').
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))

      // Now pick a different session from the sidebar. Must NOT snap back to A.
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
    })

    // Regression: closing the active session left its `?sid=` entry in history
    // and pushed the landing session on top of it. Back then landed on the dead
    // entry, the POP reader ignored it (the session is gone), and URL sync
    // pushed again — so Back never got past the closed session and Forward was
    // wiped every time. The dead entry must be overwritten instead.
    it('lets Back get past a closed session and keeps Forward', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      const urlWritesBeforeClose = seenUrls.length

      await act(async () => { await store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)
      await settle(store, [navSlots[0], navSlots[1]])
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))

      // The closed session's entry now names chat-2 (one inert Back at most);
      // the next Back must reach chat-1. Pre-fix every Back re-landed on chat-3.
      for (let i = 0; i < 3 && store.getState().chat.activeSlot !== 'chat-1-100'; i++) {
        await act(async () => { navBack() })
        await new Promise(r => setTimeout(r, 20))
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      expect(currentUrl).not.toContain('chat-3-300')

      // Forward survives: chat-1 → chat-2.
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).not.toContain('chat-3-300')
    })

    it('does not rewrite the entry of a session another tab resumed inside the close hold window', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      const urlWritesBeforeClose = seenUrls.length

      await act(async () => { await store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)

      await act(async () => { store.dispatch(sseSlots(navSlots)) })
      expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-3-300')
      await act(async () => { await store.dispatch(switchSlot('chat-1-100')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      const writesAfterBack = seenUrls.length
      await new Promise(r => setTimeout(r, 30))
      expect(seenUrls).toHaveLength(writesAfterBack)
      expect(store.getState().chat.activeSlot).toBe('chat-1-100')

      for (let i = 0; i < 3; i++) {
        await act(async () => { store.dispatch(sseSlots(navSlots)) })
      }
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).toContain('chat-3-300'))
      if (store.getState().chat.activeSlot !== 'chat-3-300') {
        await act(async () => { navBack() })
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
    })

    it('replaces the closed entry only once its hold has retired and a later list omits it', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      const urlWritesBeforeClose = seenUrls.length

      await act(async () => { await store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)

      for (let i = 0; i < 3; i++) {
        await act(async () => { store.dispatch(sseSlots([navSlots[0], navSlots[1]])) })
      }
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)

      await act(async () => { store.dispatch(sseSlots([navSlots[0], navSlots[1]])) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      expect(seenUrls).toHaveLength(urlWritesBeforeClose + 1)
      for (let i = 0; i < 2 && store.getState().chat.activeSlot !== 'chat-1-100'; i++) {
        await act(async () => { navBack() })
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    })

    // A close the server refuses brings the session back (recovery refetch), so
    // its history entry must still lead there. Overwriting it at close time, as
    // an unconditional replace would, strands the restored session behind Back.
    it('keeps the entry of a session whose close failed', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      // Hold the DELETE open across a render, as a real request is: settling it
      // in the same flush would restore the row before URL sync ever ran.
      let failClose!: (e: Error) => void
      vi.mocked(api.deleteChatSlot).mockReturnValueOnce(new Promise((_, reject) => { failClose = reject }) as never)
      vi.mocked(api.chatSlots).mockResolvedValueOnce(navSlots as never)
      let closing!: Promise<unknown>
      act(() => { closing = store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      await new Promise(r => setTimeout(r, 30))
      await act(async () => { failClose(new Error('offline')); await closing })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).toContain('chat-3-300'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))

      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
    })

    // A session closed while NOT on screen keeps its history entry. Back onto
    // it must rewrite that entry in place and keep going, not push over it.
    it('lets Back step over the entry of a session closed off screen', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      await act(async () => { await store.dispatch(deleteSlot('chat-2-200')) })
      expect(store.getState().chat.activeSlot).toBe('chat-3-300')
      await settle(store, [navSlots[0], navSlots[2]])

      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))

      await act(async () => { navForward() })
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
      expect(currentUrl).not.toContain('chat-2-200')
    })

    // A failed close whose recovery refetch is still out when Back arrives: the
    // missing row is not proof that the session was deleted, and its history
    // entry must remain available once the refetch restores it.
    it('holds a Back onto a failed close until the recovery refetch lands', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      let failClose!: (e: Error) => void
      vi.mocked(api.deleteChatSlot).mockReturnValueOnce(new Promise((_, reject) => { failClose = reject }) as never)
      let restore!: (v: unknown) => void
      vi.mocked(api.chatSlots).mockReturnValueOnce(new Promise(resolve => { restore = resolve }) as never)
      let closing!: Promise<unknown>
      act(() => { closing = store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      await new Promise(r => setTimeout(r, 30))
      await act(async () => { failClose(new Error('offline')); await closing })
      expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-3-300')

      await act(async () => { navBack() })
      await new Promise(r => setTimeout(r, 30))
      expect(store.getState().chat.activeSlot).toBe('chat-2-200')

      await act(async () => { restore(navSlots) })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).toContain('chat-3-300'))
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
    })

    // Visit 1 -> 2 -> 3, then close 2 off screen. The helper leaves the browser
    // on chat-3 with a confirmed tombstone for chat-2.
    async function closeMiddleOffScreen() {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const view = renderForPop(navSlots)
      const { store } = view
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      await act(async () => { await store.dispatch(deleteSlot('chat-2-200')) })
      await settle(store, [navSlots[0], navSlots[2]])
      return { store }
    }

    // A second Back while the first is held must be honoured, not dropped.
    it('honours a second Back pressed while a dead entry is held', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      let finishClose!: () => void
      vi.mocked(api.deleteChatSlot).mockReturnValueOnce(new Promise<void>(resolve => { finishClose = resolve }) as never)
      let closing!: Promise<unknown>
      act(() => { closing = store.dispatch(deleteSlot('chat-2-200')) })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-2-200'))

      await act(async () => { navBack() })
      await new Promise(r => setTimeout(r, 30))
      expect(currentUrl).toContain('sid=chat-2-200')
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))

      await act(async () => { finishClose(); await closing })
      await settle(store, [navSlots[0], navSlots[2]])
      await act(async () => { navForward() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
      expect(currentUrl).not.toContain('chat-2-200')
    })

    // A concurrent close answers this tab's DELETE with 404. Until the server
    // reports that close's outcome, the current history entry stays untouched.
    it('lets Back get past a session whose close DELETE returns 404 and keeps Forward', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      const urlWritesBeforeClose = seenUrls.length

      vi.mocked(api.deleteChatSlot).mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
      vi.mocked(api.chatSlots).mockResolvedValueOnce([navSlots[0], navSlots[1]] as never)
      await act(async () => { await store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)

      await act(async () => {
        store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-3-300'] }))
      })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      expect(seenUrls).toHaveLength(urlWritesBeforeClose + 1)
      await settle(store, [navSlots[0], navSlots[1]])
      expect(seenUrls).toHaveLength(urlWritesBeforeClose + 1)

      for (let i = 0; i < 3 && store.getState().chat.activeSlot !== 'chat-1-100'; i++) {
        await act(async () => { navBack() })
        await new Promise(r => setTimeout(r, 20))
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      expect(currentUrl).not.toContain('chat-3-300')

      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).not.toContain('chat-3-300')
    })

    // A post-pop recovery reply can be followed by a pre-pop WebSocket frame.
    // That unpaired straggler must not release the close hold or rewrite history.
    it('holds a 404 close through a pre-pop straggler frame', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      const urlWritesBeforeClose = seenUrls.length

      vi.mocked(api.deleteChatSlot).mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
      vi.mocked(api.chatSlots).mockResolvedValueOnce([navSlots[0], navSlots[1]] as never)
      await act(async () => { await store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)

      await act(async () => { store.dispatch(sseSlots(navSlots)) })
      expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-3-300')
      expect(store.getState().dashboard.closingSlots['chat-3-300']?.awaitingOutcome).toBe(true)
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)

      const urlCountBeforeConfirmation = seenUrls.length
      await act(async () => {
        store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-3-300'] }))
      })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      expect(seenUrls).toHaveLength(urlCountBeforeConfirmation + 1)
      await settle(store, [navSlots[0], navSlots[1]])
      expect(seenUrls).toHaveLength(urlCountBeforeConfirmation + 1)
      for (let i = 0; i < 3 && store.getState().chat.activeSlot !== 'chat-1-100'; i++) {
        await act(async () => { navBack() })
        await new Promise(r => setTimeout(r, 20))
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(seenUrls.slice(urlCountBeforeConfirmation).every(url => !url.includes('chat-3-300'))).toBe(true)
    })

    it('keeps a 404 close history entry when the competing close rolls back', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      let restore!: (v: unknown) => void
      vi.mocked(api.deleteChatSlot).mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
      vi.mocked(api.chatSlots).mockReturnValueOnce(new Promise(resolve => { restore = resolve }) as never)
      await act(async () => { await store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))

      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { navForward() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      expect(store.getState().chat.activeSlot).toBe('chat-2-200')

      await act(async () => { restore(navSlots) })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).toContain('chat-3-300'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
    })

    it('repairs a 404 close history entry after the durable removed frame', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      vi.mocked(api.deleteChatSlot).mockRejectedValueOnce(Object.assign(new Error('not found'), { status: 404 }))
      vi.mocked(api.chatSlots).mockResolvedValueOnce([navSlots[0], navSlots[1]] as never)
      await act(async () => { await store.dispatch(deleteSlot('chat-3-300')) })
      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { navForward() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      expect(store.getState().chat.activeSlot).toBe('chat-2-200')

      await act(async () => {
        store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-3-300'] }))
      })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      const writesAfterRemoved = seenUrls.length
      await settle(store, [navSlots[0], navSlots[1]])
      expect(seenUrls).toHaveLength(writesAfterRemoved)
      for (let i = 0; i < 2 && store.getState().chat.activeSlot !== 'chat-1-100'; i++) {
        await act(async () => { navBack() })
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).not.toContain('chat-3-300')
    })

    // Back onto an off-screen close while its DELETE is pending is left alone.
    // When recovery restores the session, the URL writer puts the live pane back
    // on top without rewriting the restored entry behind it.
    it('leaves a Back onto a pending close alone and keeps the restored entry reachable', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)

      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      let failClose!: (e: Error) => void
      vi.mocked(api.deleteChatSlot).mockReturnValueOnce(new Promise((_, reject) => { failClose = reject }) as never)
      let restore!: (v: unknown) => void
      vi.mocked(api.chatSlots).mockReturnValueOnce(new Promise(resolve => { restore = resolve }) as never)
      let closing!: Promise<unknown>
      act(() => { closing = store.dispatch(deleteSlot('chat-2-200')) })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-2-200'))

      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      expect(store.getState().chat.activeSlot).toBe('chat-3-300')

      await act(async () => { failClose(new Error('offline')); await closing })
      await act(async () => { restore(navSlots) })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).toContain('chat-2-200'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      expect(store.getState().chat.activeSlot).toBe('chat-3-300')

      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    })

    // Visit 1 -> 2 -> 3, then close chat-3 with its DELETE held open. The helper
    // leaves the browser on the landing (chat-2) while the URL still names chat-3.
    async function closeActiveWithPendingDelete(outcome: { resolve?: (r: () => void) => void; reject?: (r: (e: Error) => void) => void }) {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      const urlWritesBeforeClose = seenUrls.length

      vi.mocked(api.deleteChatSlot).mockReturnValueOnce(new Promise<void>((resolve, reject) => {
        outcome.resolve?.(resolve)
        outcome.reject?.(reject)
      }) as never)
      let closing!: Promise<unknown>
      act(() => { closing = store.dispatch(deleteSlot('chat-3-300')) })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      await new Promise(r => setTimeout(r, 30))
      // The landing write waits for the DELETE's outcome.
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)
      expect(store.getState().dashboard.closingSlots['chat-3-300']?.inFlightUntil).not.toBeNull()
      return { store, navSlots, closing, urlWritesBeforeClose }
    }

    it('replaces the closed entry on the durable removed frame without any later list', async () => {
      let finishClose!: () => void
      const { store, closing, urlWritesBeforeClose } = await closeActiveWithPendingDelete({
        resolve: resolve => { finishClose = resolve },
      })

      // The server publishes durable removal before the DELETE response reaches
      // this tab. No full slot list follows on a patch-capable idle connection.
      await act(async () => {
        store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-3-300'] }))
      })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      expect(seenUrls).toHaveLength(urlWritesBeforeClose + 1)
      expect(seenUrls.slice(urlWritesBeforeClose).every(url => !url.includes('chat-3-300'))).toBe(true)

      await act(async () => { finishClose(); await closing })
      expect(seenUrls).toHaveLength(urlWritesBeforeClose + 1)
      for (let i = 0; i < 2 && store.getState().chat.activeSlot !== 'chat-1-100'; i++) {
        await act(async () => { navBack() })
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).not.toContain('chat-3-300')
    })

    it('repairs a Back onto a durably removed entry without any later list', async () => {
      const navSlots = [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta'), slot('chat-3-300', 'Gamma')]
      const { store } = renderForPop(navSlots)
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
      await act(async () => { await store.dispatch(switchSlot('chat-3-300')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))

      let finishClose!: () => void
      vi.mocked(api.deleteChatSlot).mockReturnValueOnce(new Promise<void>(resolve => {
        finishClose = resolve
      }) as never)
      let closing!: Promise<unknown>
      act(() => { closing = store.dispatch(deleteSlot('chat-2-200')) })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).not.toContain('chat-2-200'))
      await act(async () => {
        store.dispatch(sseSlotPatch({ slots: [], removed: ['chat-2-200'] }))
      })
      await act(async () => { finishClose(); await closing })

      const writesBeforeBack = seenUrls.length
      await act(async () => { navBack() })
      await waitFor(() => {
        expect(seenUrls.length).toBeGreaterThan(writesBeforeBack)
        expect(currentUrl).toContain('sid=chat-3-300')
      })
      expect(store.getState().chat.activeSlot).toBe('chat-3-300')
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
      expect(currentUrl).not.toContain('chat-2-200')
    })

    // Only the close's LANDING write waits for the DELETE. A session the user
    // picks while that DELETE is still out is pushed as any other switch, so the
    // address bar names the session on screen and every stop made in that window
    // is a Back target. The closing entry left behind is repaired when Back
    // reaches it, once this tab saw the close confirmed.
    it('pushes a switch made while the close DELETE is pending and repairs the dead entry on Back', async () => {
      let finishClose!: () => void
      const { store, navSlots, closing } = await closeActiveWithPendingDelete({ resolve: r => { finishClose = r } })
      const urlWritesBeforeSwitch = seenUrls.length

      await act(async () => { await store.dispatch(switchSlot('chat-1-100')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
      expect(seenUrls.length).toBeGreaterThan(urlWritesBeforeSwitch)
      expect(store.getState().dashboard.closingSlots['chat-3-300']?.inFlightUntil).not.toBeNull()

      await act(async () => { finishClose(); await closing })
      await new Promise(r => setTimeout(r, 30))
      expect(currentUrl).toContain('sid=chat-1-100')
      await settle(store, [navSlots[0], navSlots[1]])

      // Back lands on chat-3's dead entry: repaired in place to the session on screen.
      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
      expect(store.getState().chat.activeSlot).toBe('chat-1-100')
      // Back again reaches chat-2, the stop before the close.
      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).toContain('sid=chat-2-200')
      // Forward survives, through the repaired entry.
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      expect(currentUrl).not.toContain('chat-3-300')
    })

    // Without a switch, the landing write still waits: nothing is written while
    // the DELETE is out, and the confirmation REPLACES the dead entry rather than
    // pushing over it, so Back reaches the stop before the close in one step.
    it('holds only the landing write while the close DELETE is pending', async () => {
      let finishClose!: () => void
      const { store, closing, urlWritesBeforeClose } = await closeActiveWithPendingDelete({ resolve: r => { finishClose = r } })

      await act(async () => { finishClose(); await closing })
      expect(currentUrl).toContain('sid=chat-3-300')
      expect(seenUrls).toHaveLength(urlWritesBeforeClose)
      await settle(store, [slot('chat-1-100', 'Alpha'), slot('chat-2-200', 'Beta')])
      await waitFor(() => expect(currentUrl).toContain('sid=chat-2-200'))
      // One write, and a replace: there is no entry above the landing to go Forward to.
      expect(seenUrls).toHaveLength(urlWritesBeforeClose + 1)
      await act(async () => { navForward() })
      await new Promise(r => setTimeout(r, 30))
      expect(currentUrl).toContain('sid=chat-2-200')
      expect(store.getState().chat.activeSlot).toBe('chat-2-200')

      // The replaced entry now names chat-2 (one inert Back at most); the next
      // Back must reach chat-1.
      for (let i = 0; i < 2 && store.getState().chat.activeSlot !== 'chat-1-100'; i++) {
        await act(async () => { navBack() })
        await new Promise(r => setTimeout(r, 20))
      }
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      expect(currentUrl).not.toContain('chat-3-300')
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
      expect(currentUrl).not.toContain('chat-3-300')
    })

    // A switch during the window followed by a FAILED close: the recovery list
    // brings the closed session back, and the entry its switch left behind is
    // live again — Back reaches it, and nothing was rewritten.
    it('keeps the entry left behind by a switch during a close that then fails', async () => {
      let failClose!: (e: Error) => void
      const { store, navSlots, closing } = await closeActiveWithPendingDelete({ reject: r => { failClose = r } })
      vi.mocked(api.chatSlots).mockResolvedValueOnce(navSlots as never)

      await act(async () => { await store.dispatch(switchSlot('chat-1-100')) })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
      const urlWritesBeforeFailure = seenUrls.length

      await act(async () => { failClose(new Error('offline')); await closing })
      await waitFor(() => expect(store.getState().dashboard.slots.map(s => s.key)).toContain('chat-3-300'))
      await new Promise(r => setTimeout(r, 30))
      expect(currentUrl).toContain('sid=chat-1-100')
      expect(seenUrls).toHaveLength(urlWritesBeforeFailure)

      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
      expect(currentUrl).toContain('sid=chat-3-300')
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
    })

    // `closingSlots` is a plain object, while sid is raw URL input. Inherited
    // names must not impersonate a close hold and pin activeSlot -> URL sync.
    it('does not treat a prototype URL key as an in-flight close', async () => {
      expect(closeHoldForUrl({}, 'toString')).toBeUndefined()
      expect(closeHoldForUrl({}, 'constructor')).toBeUndefined()
      expect(closeHoldForUrl({}, '__proto__')).toBeUndefined()

      const navSlots = [slot('chat-1-100', 'Alpha')]
      const { store } = renderForPop(navSlots)
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))

      await act(async () => { navTo('/chat?sid=toString') })
      expect(seenUrls).toContain('/chat?sid=toString')
      await waitFor(() => expect(currentUrl).toContain('sid=chat-1-100'))
    })

    // A reconnect clears `slotsLoaded`, but a close this tab observed as
    // confirmed remains sufficient to repair its dead history entry.
    it('holds a Back onto a dead entry across a reconnect', async () => {
      const { store } = await closeMiddleOffScreen()
      act(() => { store.dispatch(sseConnected()) })
      expect(store.getState().dashboard.slotsLoaded).toBe(false)

      await act(async () => { navBack() })
      await waitFor(() => expect(currentUrl).toContain('sid=chat-3-300'))
      expect(store.getState().dashboard.slotsLoaded).toBe(false)

      await act(async () => { navBack() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-1-100'))
      await act(async () => { navForward() })
      await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-3-300'))
    })
  })
})

// Regression: a slow slot-list load must not override a session the user
// already switched to. The deep-link ?sid activation effect runs when the slot
// list first contains the linked slot; if that arrives AFTER the user clicked a
// different session in the sidebar (switchSlot.pending sets activeSlot
// synchronously), the late activation must not snap the UI back to the
// deep-linked session.
describe('late slot-list load does not override a user switch (deep-link race)', () => {
  it('keeps the user-selected session when the deep-linked slot appears after the switch', async () => {
    // Deep-linked to chat-1-100 but the slot list is still loading (empty).
    const { store } = renderChatPage({ route: '/chat/x?sid=chat-1-100', slots: [] })
    // Deep-link can't activate yet (no slots) — activeSlot stays null.
    expect(store.getState().chat.activeSlot).toBeNull()
    // User clicks a different session in the sidebar (dispatches switchSlot directly).
    await act(async () => { await store.dispatch(switchSlot('chat-2-200')) })
    expect(store.getState().chat.activeSlot).toBe('chat-2-200')
    // The slot list now arrives (SSE), including the deep-linked chat-1-100.
    await act(async () => { store.dispatch(sseSlots(slots)) })
    // The late deep-link activation MUST NOT revert to chat-1-100.
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('chat-2-200'))
    expect(store.getState().chat.activeSlot).not.toBe('chat-1-100')
  })
})
