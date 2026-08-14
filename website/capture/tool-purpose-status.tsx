/**
 * Isolated capture entry for the session list's running-status line.
 *
 * WHY ISOLATED: the status line only exists while a turn is in flight, and the
 * bug it shows needs a tool call's SECOND websocket frame (the refinement). A
 * live gateway cannot produce that on demand — and on a host with no OS sandbox
 * it refuses to run an agent at all, so the full SPA renders its setup gate
 * instead of a chat.
 *
 * The one thing that MUST be faithful is the frame path: this stubs
 * `window.WebSocket` at the same seam the real hook opens, mounts the REAL
 * `useWebSocket()` and dispatches the REAL frames, then renders the REAL
 * `ChatSidebar`. Nothing about the label is mocked — the row resolves it exactly
 * as it does in production.
 *
 * Query string: ?refinement=with-purpose|without-purpose|no-purpose-at-all&theme=dark
 *   with-purpose       — the refinement frame carries `purpose` (what the fixed
 *                        backend broadcasts).
 *   without-purpose    — the refinement omits it (what a refinement carrying only
 *                        a refined title looks like; the frontend must keep the
 *                        purpose the initial frame supplied).
 *   no-purpose-at-all  — NEITHER frame carries one, which is the common native-tool
 *                        case (Terminal / grep / Read). The row must advance from
 *                        the initial stub title to the refined command, so this is
 *                        the scene that proves the merge did not pin the stub.
 */
import { useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { Provider } from 'react-redux'

import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { setActiveSlot } from '../src/store/chatSlice'
import { useWebSocket } from '../src/hooks/useWebSocket'
import { ThemeProvider } from '../src/hooks/useTheme'
import ChatSidebar from '../src/pages/ChatSidebar'
import type { ChatSlot } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('refinement') || 'with-purpose'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'chat-1'
const PURPOSE = 'Count the backend modules'
const REFINED_TITLE = 'Running: sleep 40; ls src/kiro_crew | wc -l'

/** Frames captured from a real turn: kiro-cli emits the initial `tool_call`
 *  with the agent's purpose, then a `tool_call_update` refinement carrying the
 *  finished title. */
const INITIAL = {
  type: 'tool_call',
  data: {
    slot: SLOT,
    tool: 'Terminal',
    kind: 'execute',
    ...(scene === 'no-purpose-at-all' ? {} : { purpose: PURPOSE }),
    input_preview: '',
    tool_call_id: 'tc-1',
  },
}
const REFINEMENT = {
  type: 'tool_call',
  data: {
    slot: SLOT,
    tool: REFINED_TITLE,
    kind: 'execute',
    input_preview: '{"command":"sleep 40; ls src/kiro_crew | wc -l"}',
    tool_call_id: 'tc-1',
    is_update: true,
    ...(scene === 'with-purpose' ? { purpose: PURPOSE } : {}),
  },
}

/** Minimal stub standing in for the gateway socket. The hook only ever reads
 *  `onmessage`/`onopen` and `readyState`, so this is the whole surface. */
class CaptureSocket {
  static OPEN = 1
  static CONNECTING = 0
  readyState = CaptureSocket.CONNECTING
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  send() {}
  close() {}
  constructor() {
    sockets.push(this)
  }
}
const sockets: CaptureSocket[] = []
;(window as unknown as { WebSocket: unknown }).WebSocket = CaptureSocket

const SLOTS: ChatSlot[] = [
  {
    key: SLOT,
    title: 'Session list purpose fix',
    messages: 12,
    running: true,
    agent: 'kirocrew',
    last_ts: new Date().toISOString(),
  },
  {
    key: 'chat-2',
    title: 'App Store editorial',
    messages: 34,
    running: false,
    agent: 'kirocrew',
    last_message: 'Both PRs are green.',
    last_ts: new Date(Date.now() - 3_600_000).toISOString(),
  },
]

function Harness() {
  useWebSocket()
  const [ready, setReady] = useState(false)
  useEffect(() => {
    store.dispatch(setActiveSlot(SLOT))
    const t = setTimeout(() => {
      const ws = sockets[0]
      if (!ws) return
      ws.readyState = CaptureSocket.OPEN
      ws.onopen?.(new Event('open'))
      const push = (frame: object) =>
        ws.onmessage?.(new MessageEvent('message', { data: JSON.stringify(frame) }))
      push(INITIAL)
      push(REFINEMENT)
      setReady(true)
    }, 300)
    return () => clearTimeout(t)
  }, [])
  return (
    <div className="flex h-screen bg-bg" data-capture-ready={ready ? '' : undefined}>
      <ChatSidebar
        slots={SLOTS}
        activeSlot={SLOT}
        unreadSlots={[]}
        history={[]}
        historyHasMore={false}
        defaultAgent="kirocrew"
        installedAgents={[{ name: 'kirocrew', description: 'Kiro Crew' }]}
      />
    </div>
  )
}

initI18n()
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
