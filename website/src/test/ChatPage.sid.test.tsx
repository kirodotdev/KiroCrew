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
import { switchSlot } from '../store/chatSlice'
import { sseSlots } from '../store/dashboardSlice'
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
vi.mock('../hooks/usePanelState', () => ({ usePanelState: () => ({ isOpen: false, openPanel: vi.fn(), closePanel: vi.fn() }), useDiffPanel: () => ({ isOpen: false, filePath: '', original: '', modified: '', openDiff: vi.fn(), closeDiff: vi.fn() }) }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Test', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: null }) }))
vi.mock('../hooks/useFilteredDropdown', () => ({ useFilteredDropdown: () => ({ filtered: [], query: '', setQuery: vi.fn(), selectedIndex: 0, setSelectedIndex: vi.fn(), onKeyDown: vi.fn() }) }))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))

// --- Stub API ---
vi.mock('../api/client', () => ({
  api: Object.fromEntries(
    ['sessions', 'chatSlotDetail', 'createChatSlot', 'deleteChatSlot', 'resumeChatSlot',
     'deleteSession', 'agentDetail', 'approveChatSlot', 'chatSlotAgent', 'chatSlotModel',
     'chatSlotWorkspace', 'models', 'planAction', 'planFromChat', 'renameSlot',
     'resolveApproval', 'screenshot', 'slackChannels', 'slackLink', 'spawnList',
     'stopChatSlot', 'uploadFiles', 'voiceSynthesize', 'workspaces', 'chatSlots',
     'notifications', 'status', 'generateTitle'].map(k => [k, vi.fn().mockResolvedValue(
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

import ChatPage from '../pages/ChatPage'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { i18nT } from '../i18n/t'
import { api } from '../api/client'

/** Slot keys `chatSlotDetail` was asked for — i.e. which sessions got fetched. */
function detailCalls(): string[] {
  return vi.mocked(api.chatSlotDetail).mock.calls.map(c => c[0] as string)
}

const slot = (key: string, title?: string, mode = ''): ChatSlot => ({
  key, title: title ?? key, messages: 0, running: false, mode, created: '', last_ts: '',
})

/** Helper to capture the current URL from MemoryRouter */
let currentUrl = ''
function UrlCapture() {
  const loc = useLocation()
  const [sp] = useSearchParams()
  currentUrl = loc.pathname + (sp.toString() ? '?' + sp.toString() : '')
  return null
}

/** Exposes the router's navigate() so tests can drive a real Back/Forward POP. */
let navBack: () => void = () => {}
let navForward: () => void = () => {}
function NavController() {
  const n = useNavigate()
  navBack = () => n(-1)
  navForward = () => n(1)
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
              <Route path="/orchestrated/:slug?" element={<ChatPage mode="orchestrator" />} />
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
  currentUrl = ''
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

const orchSlots = [
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

    /** Control for the gate above: it must DELAY the deadline to the list's
     *  arrival, not remove it. A key genuinely absent from a list that has landed
     *  still earns the banner. */
    it('still declares it missing once a list arrives without the key', async () => {
      vi.useFakeTimers()
      const { store } = renderChatPage({ route: '/chat?sid=chat-9-900', slots: [], slotsLoaded: false })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.queryByText(/session "chat-9-900" not found/i)).toBeNull()

      await act(async () => { store.dispatch(sseSlots(slots)) })
      await vi.advanceTimersByTimeAsync(5100)
      expect(screen.getByText(/session "chat-9-900" not found/i)).toBeTruthy()
      vi.useRealTimers()
    })
  })

  // Regression: loading on a chat URL (?sid= present) must not freeze switching.
  // If pendingSidRef were overloaded for both deep-link activation AND a POP in
  // flight, the deep-link load would trip the POP bail so the first switch never
  // updated the URL until a reload; loading at /chat (no ?sid) hides that.
  describe('switch after deep-link load (Mesh chat-switch bug)', () => {
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

  describe('orchestrated mode (unified view)', () => {
    it('uses /chat base path for orchestrator sessions', async () => {
      const allSlots = [...slots, ...orchSlots]
      renderChatPage({ route: '/chat', activeSlot: 'orch-1-100', slots: allSlots, mode: 'orchestrator' })
      await waitFor(() => {
        expect(currentUrl).toContain('/chat/plan-migration')
        expect(currentUrl).toContain('sid=orch-1-100')
      })
    })

    it('keeps orchestrator sessions under the unified /chat surface', async () => {
      const allSlots = [...slots, ...orchSlots]
      renderChatPage({ route: '/chat?sid=orch-1-100', slots: allSlots, mode: 'orchestrator' })
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
          status: { platform: 'darwin' }, connected: true, slots: initialSlots, approvalMode: 'normal',
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
