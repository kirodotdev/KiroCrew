/**
 * Capture entry for #26939: a background re-read of an open Dashboard tab fails.
 *
 * Mounts the REAL `CrewDynamicDashboard`. The first dashboard read answers a page;
 * every later one answers 503, as a restarting gateway would. The script then asks
 * for a re-read (`window.__rereadDashboard()`), and the tab must keep its page and
 * show the read-error band above it. ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer, { sseConnected } from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'

import { initI18n } from '../src/i18n/all'
import { ThemeProvider } from '../src/hooks/useTheme'
import CrewDynamicDashboard from '../src/pages/members/CrewDynamicDashboard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', theme)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PAGE = `<!doctype html><html><body style="font:14px system-ui;margin:16px;color:${theme === 'light' ? '#222' : '#ddd'}">
<h3 style="margin:0 0 8px">Project report</h3><p style="margin:0">Open tasks: 7 &middot; Merged this week: 12</p></body></html>`

const READ = {
  slug: 'oncall',
  member: 'oncall',
  dashboard: 'project-report',
  state: 'live',
  html: PAGE,
  rendered_html: PAGE,
  manifest: { fields: {} },
  instance_version: 1,
  template: { id: 'project-report', version: 1 },
  read: { fields: {}, version: 1, generation: 'gen-a' },
}

let reads = 0
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const ok = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } }))
  if (url.includes('/api/sandbox-doc')) {
    const { html } = JSON.parse(String(init?.body ?? '{}')) as { html: string }
    return ok({ url: URL.createObjectURL(new Blob([html], { type: 'text/html' })) })
  }
  if (url.includes('/dashboard')) {
    reads += 1
    if (reads === 1) return ok(READ)
    return Promise.resolve(new Response('{"error":"gateway restarting"}', { status: 503 }))
  }
  if (url.includes('/api/')) return Promise.resolve(new Response('{}', { status: 404 }))
  return realFetch(input, init)
}) as typeof globalThis.fetch

await initI18n()

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
;(window as unknown as { __rereadDashboard: () => Promise<void> }).__rereadDashboard = () =>
  client.refetchQueries()

// A connected socket, so the band shown is the failed-read one and not "offline".
const store = configureStore({
  reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer, instances: instancesReducer },
})
store.dispatch(sseConnected())

createRoot(document.getElementById('root')!).render(
  <Provider store={store}><MemoryRouter><QueryClientProvider client={client}>
    <ThemeProvider>
      <div className="h-screen bg-bg text-text">
        <CrewDynamicDashboard target={{ kind: 'member', slug: 'oncall', member: 'oncall' }} displayName="On Call" />
      </div>
    </ThemeProvider>
  </QueryClientProvider></MemoryRouter></Provider>,
)
