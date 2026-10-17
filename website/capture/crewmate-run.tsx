/**
 * Capture page for the crewmate DM transcript (#16617): the REAL
 * `ChatMessageList` with `createTranscriptRenderers({ crewmate })`, seeded with
 * a user message, a two-bubble crewmate run, a second user message and a lone
 * crewmate reply. What the page proves is how one message of the crewmate is
 * placed -- whether an author line (avatar + name + time) and an avatar gutter
 * precede the bubble -- so no header, composer or side panel is drawn. The
 * second user message carries inline code, a bare URL and a quote of the
 * crewmate's reply, so the page also proves the iMessage colouring (#17839): the user's bubble filled with the
 * theme accent, code, link and the quote card recoloured onto `--accent-fg`,
 * the crewmate's bubble the neutral `--bg-hover` gray with no border. The
 * last reply carries the `[STEERING …]` ack kiro-cli emits when a send landed
 * mid-turn, so the same page also proves whether a "Steered" chip closes a
 * crewmate's bubble (#17838).
 *
 * Query string: ?theme=dark|light
 *
 * Driven by scripts/capture-crewmate-run.mjs.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ChatMessageList from '../src/app-sdk/ChatMessageList'
import { createTranscriptRenderers } from '../src/pages/chat/transcriptRenderers'
import type { ChatMessage } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const CREWMATE = { name: 'kirocrew-radar', label: 'Radar' }
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/')) {
    const body = /commands|skills|agents|models|sessions|files|artifacts/.test(url) ? '[]' : '{}'
    return Promise.resolve(new Response(body, { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const row = (role: 'user' | 'assistant', content: string, ts: string, mid: string): ChatMessage =>
  ({ role, content, cls: role === 'user' ? 'msg msg-u' : 'msg', ts, meta: { mid } }) as ChatMessage

const TRANSCRIPT: ChatMessage[] = [
  row('user', 'Anything new on the nightly?', '2026-10-05T07:40:00Z', 'm1'),
  row('assistant', [
    'Shard 1 went red twice on `test_spawn_approval_crew_log`: the test takes the crew-log lock on the event loop.',
    '',
    'Builds are all green, so it is the test, not the code.',
  ].join('\n'), '2026-10-05T07:40:21Z', 'm2'),
  row('assistant', 'Filed it as #16628 with the `asyncio.to_thread` fix sketched. Waiting on triage.', '2026-10-05T07:40:24Z', 'm3'),
  // Quotes the crewmate's "Filed it as #16628" bubble, so the sent quote card
  // (muted excerpt, accent bar, bordered surface) is drawn INSIDE the accent
  // bubble too.
  {
    ...row('user', 'Good. And the dispatcher? Check `push_slot.py` first, then https://github.com/kirodotdev/KiroCrew/issues/17839', '2026-10-05T07:52:00Z', 'm4'),
    meta: { mid: 'm4', quote: { role: 'assistant', text: 'Filed it as #16628 with the `asyncio.to_thread` fix sketched. Waiting on triage.', ts: '2026-10-05T07:40:24Z', mid: 'm3' } },
  },
  // "And the dispatcher?" landed while the crewmate was still working, so its
  // reply carries kiro-cli's inline steer acknowledgement.
  row('assistant', 'Back up since 08:22Z: it no longer mints an owner token, it presents the cron\'s own credential. First cycle dispatched 4 of the backlog.\n\n[STEERING steer-4f2a: answered the dispatcher question first]', '2026-10-05T08:24:02Z', 'm5'),
]

function Scene() {
  return (
    <div className="bg-bg text-text flex flex-col" style={{ height: '100vh' }} data-capture-root>
      <div className="flex-1 min-h-0 flex flex-col" data-testid="crewmate-host">
        <ChatMessageList
          messages={TRANSCRIPT}
          running={false}
          renderers={createTranscriptRenderers({ slot: 'chat-1', hideSteerBadge: true, crewmate: CREWMATE, crewmateTranscript: TRANSCRIPT })}
          threads={{ summaryOf: () => undefined, onOpen: () => {}, crewmateName: CREWMATE.name }}
        />
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <Scene />
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
