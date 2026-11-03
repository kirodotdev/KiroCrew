/**
 * Capture page: the side panel's tab strip in an Ultra Mode chat and in a normal chat.
 *
 * The REAL `SidePanel` and `usePanelTabs`, with a Dashboard tab stored on the
 * strip the way turning Ultra Mode on leaves it. `?slot=crew-root` is an Ultra Mode
 * root, `?slot=plain` a normal chat. The Ultra Mode preview is on. Every `/api/`
 * read answers `{}`.
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import { sseSlots } from '../src/store/dashboardSlice'
import { PREVIEW_CREW_MODE } from '../src/utils/previewFlags'
import SidePanel from '../src/pages/chat/SidePanel'
import { usePanelTabs } from '../src/hooks/usePanelTabs'

initI18n()
const params = new URLSearchParams(location.search)
const slot = params.get('slot') === 'plain' ? 'plain' : 'crew-root'
localStorage.setItem('mc-theme', 'dark')
localStorage.setItem(PREVIEW_CREW_MODE, '1')
document.documentElement.setAttribute('data-theme', 'kiro-dark')

const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  return Promise.resolve(new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
}

store.dispatch(sseSlots([
  { key: 'crew-root', title: 'Tea shop launch', messages: 3, crew_mode_root: true, agent: 'kirocrew-conductor', agent_kind: 'template' },
  { key: 'plain', title: 'Plain chat', messages: 3, agent: 'kirocrew', agent_kind: 'template' },
] as never))

function Harness() {
  const tabsCtl = usePanelTabs(slot)
  const { openView, tabs } = tabsCtl
  useEffect(() => {
    if (!tabs.some(t => t.kind === 'command-center')) openView('command-center')
  }, [openView, tabs])
  return (
    <div style={{ width: 820, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }} className="bg-bg text-text" data-testid="capture-frame">
      <SidePanel tabsCtl={tabsCtl} slot={slot} onFileSave={async () => {}} onClose={() => {}} />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider><Harness /></ThemeProvider>
      </QueryClientProvider>
    </Provider>
  </MemoryRouter>,
)
