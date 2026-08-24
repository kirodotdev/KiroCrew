/**
 * Isolated capture entry for the proactive "update found" popup and the
 * top-bar update pill.
 *
 * WHY ISOLATED: the popup only opens off a live update event plus a config
 * read, which in the real shell needs Electron's preload or a gateway whose
 * feed genuinely has a newer build. Here the REAL components render against
 * the real stylesheet and the real api client — only the transport is stubbed
 * (window.fetch answers the two config/check routes with canned JSON), and
 * the desktop lifecycle event is seeded into the same ['update-state'] query
 * cache the Electron subscription writes.
 *
 * Scenes (?scene=):
 *   desktop  — Electron `found` payload → Download primary action
 *   command  — gateway wheel install → copyable installer command
 *   apply    — gateway git checkout → Update now primary action
 *   pill / pill-downloading / pill-downloaded — the top-bar pill states
 * Plus ?theme=dark|light and ?lang=en|zh-CN.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'

import { initI18n } from '../src/i18n/all'
import dashboardReducer, { sseStatus, setDesktopUpdateAvailable } from '../src/store/dashboardSlice'
import chatReducer from '../src/store/chatSlice'
import notificationsReducer from '../src/store/notificationsSlice'
import instancesReducer from '../src/store/instancesSlice'
import UpdateFoundModal from '../src/components/UpdateFoundModal'
import UpdatePill from '../src/components/UpdatePill'
import type { StatusData } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'desktop'
const theme = params.get('theme') || 'dark'
const lang = params.get('lang') || 'en'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

// Realistic length and shape: gateway notes are CHANGELOG.md markdown, so the
// frame must prove headers/lists render and the box scrolls.
const NOTES = [
  '### Highlights',
  '',
  '- The chat thinking-block header now settles to \u201cThought process\u201d once reasoning stops',
  '- Discover page gains a 1200px content width cap; category picks keep the editorial spotlight',
  '- The update popup you are looking at: per-version remind-tomorrow / skip, atomic persistence',
  '',
  '### Fixes',
  '',
  '- Queued-message cancel restores your typed text instead of serialization markers',
  '- Touch devices get a Paste soft key in the terminal',
  '- MCP servers are probed instead of assumed, so the panel reports what is genuinely usable',
  '',
  '### Before you upgrade',
  '',
  '- Updates follow **Stable** by default; re-opt into Insider from Settings \u2192 About',
].join('\n')

// Canned transport: the api client's real fetch paths, answered locally so the
// frame exercises the component's genuine data flow (config gate + lazy notes).
const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = String(typeof input === 'string' ? input : input instanceof URL ? input : (input as Request).url)
  const json = (body: unknown) =>
    Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  if (url.includes('/api/config/kirocrew')) return json({ dashboard: { update_nudge: {} } })
  if (url.includes('/api/update/check')) return json({ changes: NOTES, latest_version: '0.5.0' })
  return realFetch(input, init)
}) as typeof window.fetch

const store = configureStore({
  reducer: {
    dashboard: dashboardReducer,
    chat: chatReducer,
    notifications: notificationsReducer,
    instances: instancesReducer,
  },
})
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

if (scene === 'desktop') {
  // The candidacy gate requires a preload that can download; the frame only
  // needs the function to exist, never to run.
  ;(window as unknown as { updateAPI?: object }).updateAPI = { download: async () => undefined }
  store.dispatch(setDesktopUpdateAvailable(true))
  queryClient.setQueryData(['update-state'], { state: 'found', version: '0.5.0', notes: NOTES })
} else if (scene === 'command') {
  store.dispatch(sseStatus({
    update_available: true, update_latest_version: '0.5.0', update_can_apply: false,
    update_command: 'curl -fsSL https://download.crew.kiro.dev/cli.sh | sh',
  } as StatusData))
} else if (scene === 'apply') {
  store.dispatch(sseStatus({
    update_available: true, update_latest_version: '0.5.0', update_can_apply: true,
  } as StatusData))
} else if (scene.startsWith('pill')) {
  store.dispatch(setDesktopUpdateAvailable(true))
  if (scene === 'pill-downloading') {
    queryClient.setQueryData(['update-state'], { state: 'downloading', version: '0.5.0', percent: 42 })
  } else if (scene === 'pill-downloaded') {
    queryClient.setQueryData(['update-state'], { state: 'downloaded', version: '0.5.0' })
  }
}

initI18n(lang)
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <Provider store={store}>
      <MemoryRouter>
        {scene.startsWith('pill') ? (
          // The pill floats in a header-like strip so the frame shows its
          // real 28px chrome against the top-bar background token.
          <div className="flex items-center justify-end gap-2 p-3 bg-bg-elevated" style={{ width: 360 }}>
            <UpdatePill />
          </div>
        ) : (
          <div style={{ width: '100vw', height: '100vh' }} className="bg-bg">
            <UpdateFoundModal />
          </div>
        )}
      </MemoryRouter>
    </Provider>
  </QueryClientProvider>,
)
