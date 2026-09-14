/**
 * Isolated capture entry: recall a prompt whose send never reached the transcript.
 *
 * ONLY THE NETWORK IS SIMULATED, by the capture script via `page.route`, so the
 * browser's own fetch carries real abort semantics: the send POST hangs forever
 * and slot-detail returns the original transcript, as a server that never got the
 * POST has no row for it. Everything else is shipped code — the composer clear,
 * the `recordSendAttempt` dispatch, the real `refreshSlot` thunk and its wholesale
 * `state.messages =` replacement, ChatInput's ArrowUp handler, the REAL ChatPage.
 *
 * The readout panel makes the delta visible: it is a string present in one place
 * and absent from another. It must not import `buildRecallHistory` — this file is
 * copied into a pre-fix worktree where that module is absent, and the pair proves
 * nothing if the two runs are not the same harness. Fixture text is synthetic.
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { Provider, useDispatch, useSelector } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n'
import chatReducer, { refreshSlot } from '../src/store/chatSlice'
import dashboardReducer from '../src/store/dashboardSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import type { RootState } from '../src/store'
import ChatPage from '../src/pages/ChatPage'
import '../src/index.css'

initI18n('en')

const SLOT = 'demo-slot'
/** A prompt that DID land, so recall history is non-empty before the test. Its
 *  position proves the fix appends rather than reordering. */
const PRIOR_PROMPT = 'summarise the release notes'
const PRIOR_REPLY =
  'Three entries: a faster cold start, a fix for duplicate retries, and a new export button.'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
// ThemeProvider rewrites data-theme on mount from localStorage 'mc-theme', so
// seeding only the attribute renders every frame in the system theme instead.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme)

// Deterministic pixels: no entrance eases, no pulsing dots, no smooth scroll.
const style = document.createElement('style')
style.textContent =
  '*, *::before, *::after { animation: none !important; transition: none !important; scroll-behavior: auto !important; }'
document.head.appendChild(style)

type Msg = {
  role: string
  content: string
  cls: string
  ts: string
  meta?: Record<string, unknown>
}

/** The transcript the server holds. `meta.mid` is the server's own per-row
 *  stamp; the optimistic bubble the composer appends carries none, which is
 *  exactly why a wholesale refresh cannot retain it. */
const SERVER_TRANSCRIPT: Msg[] = [
  { role: 'user', content: PRIOR_PROMPT, cls: '', ts: '2026-04-02T09:14:00Z', meta: { mid: 'm-1' } },
  { role: 'assistant', content: PRIOR_REPLY, cls: '', ts: '2026-04-02T09:14:06Z', meta: { mid: 'm-2' } },
]

// ChatPage's mount refetch REPLACES chat.messages, so the script's route fixture
// must be byte-equal to this one; published here so the script can enforce that.
;(window as unknown as { __CAPTURE_FIXTURE__: Msg[] }).__CAPTURE_FIXTURE__ = SERVER_TRANSCRIPT
;(window as unknown as { __CAPTURE_SLOT__: string }).__CAPTURE_SLOT__ = SLOT

const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  preloadedState: {
    dashboard: {
      status: null,
      // ChatInput refuses to send when `connected` is false; the tunnel this
      // scenario simulates dies mid-send, so the UI still believes it is up.
      connected: true,
      slots: [
        {
          key: SLOT,
          title: 'release prep',
          messages: SERVER_TRANSCRIPT.length,
          running: false,
          mode: '',
          pending_approval: false,
          waiting_for_input: false,
          last_activity_ts: undefined,
        },
      ],
      slotsLoaded: true,
      unreadSlots: [],
      refreshTrigger: 0,
      approvalMode: 'normal',
      subagentRunning: {},
      subagentDetails: {},
      subagentText: {},
    } as unknown as RootState['dashboard'],
    chat: {
      activeSlot: SLOT,
      messages: SERVER_TRANSCRIPT,
      slotRunning: false,
      slotStopping: false,
      slotState: 'idle',
      history: [],
      historyHasMore: false,
      pendingInput: null,
      subagents: {},
      toolLog: [],
      activityOpen: false,
      activityTab: 'tools',
      slotHasMore: false,
      slotOldestIndex: 0,
      loadingOlder: false,
      slotCursorKey: null,
      slotStatusDetail: {},
      slotContextPct: {},
      slotActivity: {},
      slotHistory: [],
      historyOffset: 0,
      _wsChunkedDuringFetch: false,
      slotMessages: {},
      slotLoading: false,
    } as unknown as RootState['chat'],
    notifications: { items: [] } as unknown as RootState['notifications'],
  },
})

/** Live mirror of the REAL composer's controlled value, read off the rendered
 *  textarea rather than reimplemented, so the panel cannot disagree with what
 *  the viewer is looking at. */
function useComposerValue(): string {
  const [v, setV] = useState('')
  useEffect(() => {
    let raf = 0
    const tick = () => {
      const el = document.querySelector<HTMLTextAreaElement>('textarea[data-composer-input]')
      setV(prev => (el && el.value !== prev ? el.value : prev))
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [])
  return v
}

function Readout() {
  const dispatch = useDispatch()
  const composer = useComposerValue()
  const messages = useSelector((s: RootState) => s.chat.messages)
  // Optional chaining is load-bearing: the pre-fix slice has no `attemptedSends`.
  const attempted = useSelector(
    (s: RootState) =>
      (s.chat as { attemptedSends?: Record<string, { text: string; sendId: string }[]> }).attemptedSends?.[SLOT],
  )
  const prompts = messages.filter(m => m.role === 'user').map(m => m.content ?? '')
  const cell: React.CSSProperties = { color: 'var(--text-strong)' }
  const label: React.CSSProperties = { color: 'var(--muted)' }
  return (
    <div
      data-testid="recall-readout"
      style={{
        flex: '0 0 auto',
        padding: '9px 14px',
        borderTop: '1px solid var(--border)',
        background: 'var(--bg-hover)',
        font: '12.5px/1.65 ui-monospace, monospace',
        color: 'var(--text)',
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
      }}
    >
      <div>
        <span style={label}>composer value: </span>
        <span data-testid="readout-composer" style={cell}>
          {composer === '' ? '(empty)' : composer}
        </span>
      </div>
      <div>
        <span style={label}>transcript prompts ({prompts.length}): </span>
        <span data-testid="readout-transcript-prompts" style={cell}>
          {prompts.join('  ·  ') || '(none)'}
        </span>
      </div>
      <div>
        <span style={label}>submitted prompts the store recorded: </span>
        <span data-testid="readout-attempts" style={cell}>
          {attempted?.length ?? 0}
        </span>
      </div>
      <button
        data-testid="harness-reconnect"
        onClick={() => dispatch(refreshSlot(SLOT) as never)}
        style={{
          alignSelf: 'flex-start',
          marginTop: 5,
          padding: '3px 9px',
          borderRadius: 6,
          border: '1px solid var(--border)',
          background: 'var(--bg-elevated)',
          color: 'var(--text)',
          font: '12px ui-monospace, monospace',
          cursor: 'pointer',
        }}
      >
        simulate reconnect — dispatch the real refreshSlot thunk
      </button>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <div
            data-capture-root
            style={{ height: '100vh', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}
          >
            <div style={{ flex: '1 1 auto', minHeight: 0, display: 'flex', flexDirection: 'column' }}>
              <ChatPage />
            </div>
            <Readout />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
