/**
 * Isolated capture entry for the side panel's PERMANENT pinned block (#5377).
 *
 * WHY ISOLATED: the subject is what the strip renders when the session has NO
 * content at all. That state is trivial to reach in isolation and awkward to
 * reach in a live gateway, where a real chat almost always has something.
 *
 * WHAT IS FAITHFUL: the REAL `SidePanel`, mounting the real `usePanelTabs`
 * store, so the strip comes from the component's own
 * `useEffect(() => { syncPinned(PINNED_VIEWS) })` rather than from a fixture.
 * Nothing is seeded: no pins, no sources, no issues, no artifacts. The API is
 * stubbed at the fetch boundary so no code path hangs on a gateway that does
 * not exist here.
 *
 * `?claimed=on` calls `syncPinned([])` AFTER mount, which is the behaviour the
 * hook's contract used to be read as - content-gated, so an empty session shows
 * an empty strip. SidePanel's effect is keyed on the callback identity and has
 * already run by then, so it does not re-add them. One harness therefore
 * captures both arms, and the difference is asserted rather than asserted-about.
 *
 * Query string: ?theme=dark&claimed=off
 */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
// Initialise i18next exactly as main.tsx does - without it every label in the
// frame is blank and the screenshot misrepresents the real UI.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { store } from '../src/store'
import SidePanel from '../src/pages/chat/SidePanel'
import { usePanelTabs } from '../src/hooks/usePanelTabs'

initI18n()

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const claimed = params.get('claimed') === 'on'
// `ThemeProvider` is the authority: it reads `mc-theme` and applies the palette
// itself, so setting `data-theme` alone is clobbered on mount (an unset
// preference resolves to `system`, which is LIGHT in headless Chromium). Seed
// the preference it reads, and set the attribute too so the first paint before
// the provider's effect is already the right palette.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Fetch stub at the API boundary: every dashboard read answers an empty
 *  payload, so the panel renders its genuine no-content state instead of a
 *  spinner waiting on a gateway. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  return Promise.resolve(new Response('{}', {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
}

function Harness() {
  const tabsCtl = usePanelTabs('capture-slot')
  const { syncPinned } = tabsCtl
  useEffect(() => {
    if (!claimed) return
    // Run after SidePanel's own mount effect, so this models "the strip is
    // reconciled to what has content" rather than racing the real call.
    const id = setTimeout(() => syncPinned([]), 0)
    return () => clearTimeout(id)
  }, [syncPinned])
  return (
    // A right-dock-sized frame, the width the panel occupies in the chat.
    <div style={{ width: 460, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }} className="bg-bg text-text">
      <SidePanel
        tabsCtl={tabsCtl}
        slot="capture-slot"
        onFileSave={async () => {}}
        onClose={() => {}}
      />
    </div>
  )
}

/** The tab strip, read from the rendered chips by role rather than by any
 *  class chain, so the driver never encodes SidePanel's internals. A pinned
 *  chip is icon-only when inactive, so its name is the aria-label. */
;(window as unknown as { __strip: () => unknown }).__strip = () => {
  const chips = [...document.querySelectorAll<HTMLElement>('[role="tab"]')]
  return {
    count: chips.length,
    names: chips.map(c => c.getAttribute('aria-label') || c.textContent?.trim() || ''),
    closable: chips.map(c => c.querySelectorAll('button').length > 0),
  }
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Provider store={store}>
      <ThemeProvider>
        <MemoryRouter>
          <Harness />
        </MemoryRouter>
      </ThemeProvider>
    </Provider>
  </QueryClientProvider>,
)
