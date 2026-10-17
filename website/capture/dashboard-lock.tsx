/**
 * Isolated capture entry for the dashboard structure lock badge (PR #18687).
 *
 * Mounts the REAL CrewDynamicDashboard with the lock live: the API stub
 * answers `structure_locked: true` so the badge is always shown. Both
 * themes are driven via the query string (?theme=dark|light). The entry
 * mirrors crew-dashboard-states.tsx but uses the target-object API that
 * main already ships.
 *
 * Usage: driven by capture-dashboard-lock.mjs — do not open directly.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import CrewDynamicDashboard from '../src/pages/members/CrewDynamicDashboard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const locked = params.get('locked') !== '0'

localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const LIVE_PAGE = {
  instance_version: 2,
  template: { id: 'fixture-board', version: 1 },
  // The rendered_html includes the readiness beacon so the component transitions
  // from the "awaiting first page" state to the live frame view, making the
  // locked badge appear ABOVE the rendered content rather than over an error view.
  html: '<!doctype html><title>Board</title><p>Board</p>',
  rendered_html: '<!doctype html><title>Board</title><script>window.parent.postMessage({type:"kirocrew-dashboard:ready"},"*")</script><p>Board</p>',
  manifest: {
    id: 'fixture-board',
    version: 1,
    title: 'Board',
    fields: [],
  },
  state: 'live' as const,
  structure_locked: locked,
}

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/dashboard')) {
    return Promise.resolve(
      new Response(JSON.stringify(LIVE_PAGE), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  }
  if (url.includes('/api/sandbox-doc')) {
    // Serve the minted document as a blob URL so the iframe renders real content.
    // The body is always a JSON string (post() uses JSON.stringify), so no await needed.
    const text = typeof init?.body === 'string' ? init.body : '{}'
    const { html = '' } = JSON.parse(text) as { html?: string }
    const blob = new Blob([html], { type: 'text/html' })
    const blobUrl = URL.createObjectURL(blob)
    return Promise.resolve(new Response(JSON.stringify({ url: blobUrl }), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 404 }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

await initI18n()

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={client}>
    <ThemeProvider>
      <div className="h-screen bg-bg text-text">
        <CrewDynamicDashboard
          target={{ kind: 'member', slug: 'oncall', member: 'oncall' }}
          displayName="On Call"
          // Provide the callback so the badge renders as a clickable button,
          // exactly as it does on the Members page.
          onOpenProfileTab={() => { /* navigate to profile tab — no-op in capture */ }}
        />
      </div>
    </ThemeProvider>
  </QueryClientProvider>,
)
