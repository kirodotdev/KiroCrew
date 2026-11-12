/**
 * Isolated capture entry for #35598: a chat widget's toolbar and the notice it
 * shows when "Open in new tab" is refused. `window.open` is stubbed to return
 * null, which is what a popup blocker or a denying desktop shell answers.
 *
 * Usage (in website/): start `npx vite --host 127.0.0.1` on a free local port,
 * then open /capture/widget-toolbar-popout.html?theme=dark|light.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import WidgetFrame from '../src/components/WidgetFrame'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
document.documentElement.setAttribute('data-mode', theme)

window.open = () => null

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/sandbox-doc')) {
    const html = JSON.parse(String(init?.body ?? '{}')).html ?? ''
    const doc = URL.createObjectURL(new Blob([html], { type: 'text/html' }))
    return Promise.resolve(new Response(JSON.stringify({ url: doc }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  if (url.includes('/api/')) {
    return Promise.resolve(new Response('{}', { status: 404, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

const WIDGET = `<div style="padding:16px;font:14px system-ui;color:var(--text)">
  <div style="font-weight:600">Deploy status</div>
  <div style="color:var(--muted)">All four stages are green.</div>
</div>`

function Scene() {
  return (
    <div className="min-h-screen bg-bg text-text px-8 py-6">
      <div className="max-w-[760px] mx-auto">
        <WidgetFrame html={WIDGET} title="Deploy status" messageTs="1700000000.001" />
      </div>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <ThemeProvider><Scene /></ThemeProvider>
  </QueryClientProvider>,
)
