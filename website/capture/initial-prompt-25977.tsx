/**
 * Isolated capture entry for #25977: the session header menu's "Initial prompt"
 * submenu (the session's first prompt, Copy prompt, Draft a new session from
 * this prompt).
 *
 * Mounts the REAL `ChatHeaderMenu` (the session header's dropdown) for one
 * seeded, active session. Nothing inside the menu is mocked: only the network
 * and the clipboard are stubbed, and every request the menu makes is recorded on
 * `window.__requests` so the script can assert what a click sent.
 *
 * The transcript is served the way the gateway serves it: `GET
 * /api/chat/slots/<key>?limit=50&before=50` returns the OLDEST rows, so the first
 * prompt is found even when the open pane holds only the newest page.
 *
 * Query string: ?theme=dark|light, &state=ok|long|empty|sent|fail|failonce|slow
 * (failonce: the first read fails, every later read succeeds; slow: the read
 * answers after 4s), &clip=fail,
 * &create=fail (the create POST answers 500), &agent=fail (the agent pick on the
 * new session answers 409), &agent=slow (the agent pick answers after 1.5s, so
 * the script can open another session meanwhile), &storage=fail (the staged
 * prefill write is refused).
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { setActiveSlot } from '../src/store/chatSlice'
import { ThemeProvider } from '../src/hooks/useTheme'
import { ChatHeaderMenu } from '../src/pages/chat/ChatHeaderMenu'
import type { ChatSlot } from '../src/types'
import { setGatewayConnected } from '../src/utils/errorReport'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const state = params.get('state') || 'ok'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

const SLOT = 'chat-42-1700000000'
const NEW_SLOT = 'chat-43-1700000500'
const OTHER_SLOT = 'chat-41-1699999000'
const FIRST_PROMPT = 'Investigate why the composer loses its draft when I switch sessions.\n\nConstraints:\n- keep the fix inside website/src/pages/chat\n- no new dependencies'
const LONG_PROMPT = Array.from({ length: 24 }, (_, i) => `Step ${i + 1}: read the next part of the transcript, then summarise what changed and why it matters for the release notes.`).join('\n')

// Oldest rows first, as the gateway pages them: a machine row, then the person's
// first prompt (with a later prompt the menu must NOT pick).
let transcriptReads = 0
let failedReads = 0
function transcript(): unknown[] {
  transcriptReads += 1
  // `sent`: the first read finds an empty session, every later read finds the
  // prompt the person has since sent.
  if (state === 'sent' && transcriptReads === 1) return [{ role: 'system', content: 'Session started', cls: 'system' }]
  if (state === 'empty') return [{ role: 'system', content: 'Session started', cls: 'system' }]
  const first = state === 'long' ? LONG_PROMPT : FIRST_PROMPT
  return [
    { role: 'system', content: 'Session started', cls: 'system' },
    { role: 'user', content: first, cls: 'user', ts: '2026-11-01T09:00:00Z' },
    { role: 'assistant', content: 'Looking at the draft-restore effect now.', cls: 'assistant' },
    { role: 'user', content: 'Now also check the popout window.', cls: 'user', ts: '2026-11-01T09:05:00Z' },
  ]
}

declare global { interface Window { __requests: { method: string; url: string; body?: unknown }[]; __clipWrites: string[] } }
window.__requests = []
const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const method = (init?.method || 'GET').toUpperCase()
  let body: unknown
  try { body = init?.body ? JSON.parse(String(init.body)) : undefined } catch { body = init?.body }
  window.__requests.push({ method, url, body })
  if (method === 'GET' && url.includes(`/api/chat/slots/${SLOT}?`)) {
    if (state === 'fail') return json({ error: 'history_corpus_unreadable' }, 500)
    if (state === 'failonce' && failedReads++ === 0) return json({ error: 'history_corpus_unreadable' }, 500)
    if (state === 'slow') { const rows = transcript(); return new Promise<Response>(done => setTimeout(() => void json({ key: SLOT, running: false, messages: rows, total: rows.length, has_more: false, next_before: 0 }).then(done), 4000)) }
    const rows = transcript()
    return json({ key: SLOT, running: false, messages: rows, total: rows.length, has_more: false, next_before: 0 })
  }
  if (method === 'POST' && /\/api\/chat\/slots$/.test(url.split('?')[0])) {
    if (params.get('create') === 'fail') return json({ error: 'Too many sessions are open on this gateway.' }, 500)
    return json({ key: NEW_SLOT, title: '', messages: 0, running: false, agent: (body as { agent?: string })?.agent })
  }
  if (method === 'POST' && url.split('?')[0].endsWith(`/api/chat/slots/${NEW_SLOT}/agent`) && params.get('agent') === 'fail') {
    return json({ error: 'the selected agent choice is not available', code: 'agent_choice_unavailable' }, 409)
  }
  if (method === 'POST' && url.split('?')[0].endsWith(`/api/chat/slots/${NEW_SLOT}/agent`) && params.get('agent') === 'slow') {
    return new Promise<Response>(done => setTimeout(() => void json({ ok: true }).then(done), 1500))
  }
  if (/folders|commands|skills|agents|models|sessions|files|artifacts|mcp/.test(url)) return json([])
  return json({})
}) as typeof globalThis.fetch

window.__clipWrites = []
const clipFails = params.get('clip') === 'fail'
Object.defineProperty(navigator, 'clipboard', {
  configurable: true,
  value: {
    writeText: (text: string) => clipFails
      ? Promise.reject(new DOMException('denied', 'NotAllowedError'))
      : (window.__clipWrites.push(text), Promise.resolve()),
  },
})
if (clipFails) document.execCommand = () => false
// &storage=fail: sessionStorage refuses the staged prefill write.
if (params.get('storage') === 'fail') {
  const realSet = Storage.prototype.setItem
  Storage.prototype.setItem = function (key: string, value: string) {
    if (this === sessionStorage && key === 'kirocrew_prefill') throw new DOMException('quota', 'QuotaExceededError')
    return realSet.call(this, key, value)
  }
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

store.dispatch(sseSlots([{
  key: SLOT, title: 'Composer draft investigation', messages: 4, running: false,
  agent: 'bug-resolve', agent_kind: 'member', folder_id: 'f-kirocrew',
  // &noproject=1: a session with no project directory (the one-call create).
  ...(params.get('noproject') ? {} : { project: '/home/dev/KiroCrew' }),
} as ChatSlot, ...(params.get('agent') === 'slow'
  // &agent=slow: a second listed session the user can open while the create runs.
  ? [{ key: OTHER_SLOT, title: 'Release notes draft', messages: 2, running: false, folder_id: 'f-kirocrew' } as ChatSlot]
  : [])]))
store.dispatch(setActiveSlot(SLOT))
// A live gateway, so each error notice's "Ask the agent" hand-off row renders.
setGatewayConnected(true)

declare global { interface Window { __store: typeof store; __openOther: () => void } }
window.__store = store
// The user opening another session: the same action a sidebar row's click ends in.
window.__openOther = () => { store.dispatch(setActiveSlot(OTHER_SLOT)) }

function Scene() {
  return (
    <div className="bg-bg text-text" style={{ height: '100vh', padding: '16px 24px' }} data-capture-root>
      <div className="flex items-center gap-1">
        <span className="text-[15px] font-semibold text-text-strong">Composer draft investigation</span>
        <ChatHeaderMenu activeSlot={SLOT} agent="bug-resolve" mode="" />
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/chat']}>
          <Scene />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
