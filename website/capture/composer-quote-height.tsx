/**
 * Isolated capture entry for #35680: the Lexical composer (Style Markdown While
 * Typing) holding four quoted passages, each with a short comment, under a
 * transcript. Before the fix the editor grows to half the window; after, it
 * stops at the autosize cap and scrolls inside itself.
 *
 * Usage (in website/): start `npx vite` on a free local port, then open
 * /capture/composer-quote-height.html?theme=dark|light in a 1100x720 window.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ChatInput from '../src/components/ChatInput'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { quoteIntoDraft } from '../src/chat-core/composer/quoteDraft'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
localStorage.removeItem('mc-input-height')
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

const REPLY = [
  'Here is the rollout plan for the Acme billing service:',
  '',
  '1. Ship the schema change behind a flag in the staging region.',
  '2. Backfill the invoice table in batches of 5,000 rows.',
  '3. Turn the flag on for 5% of accounts and watch error rates for a day.',
  '4. Ramp to 100% once p99 latency stays under 300 ms.',
  '',
  'The backfill is the slow part: about four hours at current volume.',
].join('\n')
const PASSAGES = [
  ['Ship the schema change behind a flag in the staging region.', 'Which flag service?'],
  ['Backfill the invoice table in batches of 5,000 rows.', 'Can we go bigger at night?'],
  ['Turn the flag on for 5% of accounts and watch error rates for a day.', 'One day feels short.'],
  ['Ramp to 100% once p99 latency stays under 300 ms.', 'Agreed.'],
]
let seeded = ''
for (const [q, c] of PASSAGES) seeded = quoteIntoDraft(seeded, q) + c

function Scene() {
  const [draft, setDraft] = useState(seeded)
  return (
    <div className="h-screen flex flex-col bg-bg text-text">
      <div className="flex-1 min-h-0 overflow-y-auto px-8 py-6" data-testid="transcript">
        {[0, 1, 2].map(i => <div key={i} className="mb-6 max-w-[900px] mx-auto"><MarkdownRenderer content={REPLY} /></div>)}
      </div>
      <div className="shrink-0 px-4 pb-4">
        <ChatInput value={draft} onChange={setDraft} onSend={() => setDraft('')} connected lexicalComposer inlineMarkdown />
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
