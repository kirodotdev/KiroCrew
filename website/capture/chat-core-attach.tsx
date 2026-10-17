import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AppApiProvider, ChatEmbed } from '../src/app-sdk/index'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

// Fake data only. No real chat or upload reaches a gateway.
const params = new URLSearchParams(location.search)
const mode = params.get('mode') ?? 'dark'
const deny = params.has('deny')
const refuse = params.has('refuse')
const running = params.has('running')
document.documentElement.dataset.theme = mode === 'light' ? 'kiro-light' : 'kiro-dark'
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
window.fetch = async (input, init) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.origin)
  if (url.pathname === '/api/chat/slots/attach-capture') return json({
    title: 'Review a sample file', running,
    messages: [{ role: 'assistant', content: 'Add the sample file when you are ready.', cls: '', ts: '2026-01-01T12:00:00Z' }],
  })
  if (url.pathname === '/api/upload/file') return json({ paths: ['/sample/example.pdf'] })
  if (url.pathname === '/api/chat' && init?.method === 'POST') return refuse
    ? json({ error: 'Sample send refused' }, 409)
    : json({ ok: true, queued: running, dispatched: !running })
  return json({})
}
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <AppApiProvider appName="capture" allowedApiPaths={deny ? ['/api/chat'] : ['/api/chat', '/api/upload/file']} allowedEvents={[]} subscribeFn={() => () => {}} navigateFn={() => {}} notifyFn={() => {}}>
      <div data-capture-root style={{ width: '100%', maxWidth: 560, height: 440, margin: '16px auto', padding: 12, boxSizing: 'border-box', background: 'var(--bg)' }}>
        <ChatEmbed slotKey="attach-capture" />
      </div>
    </AppApiProvider>
  </QueryClientProvider>,
)
