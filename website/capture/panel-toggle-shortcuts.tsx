/**
 * Isolated capture entry for the panel-toggle shortcut surfaces (PR #4490).
 *
 * WHY ISOLATED: reaching /settings?tab=shortcuts (or the Alt+K modal) through
 * the full SPA needs a live gateway plus a dashboard credential — without one
 * the shell renders the Kiro CLI prerequisite gate instead. This mounts the
 * REAL ShortcutsPanel / ShortcutsModal against the REAL stylesheet and theme
 * tokens. Both surfaces read only localStorage, so no API seeding is needed.
 *
 * Scene + theme come from the query string: ?scene=settings&theme=dark
 * Scenes:
 *   settings          — Settings → Shortcuts panel, factory defaults
 *   settings-custom   — same panel with a recorded custom chord + a cleared toggle
 *   modal             — the Alt+K modal listing the panel toggles
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import ShortcutsModal from '../src/components/ShortcutsModal'
import { ShortcutsPanel } from '../src/pages/settings/ShortcutsPanel'
import { PANEL_TOGGLE_SHORTCUTS_KEY } from '../src/lib/panelToggleShortcuts'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'settings'
const theme = params.get('theme') || 'dark'

document.documentElement.dataset.theme = theme
document.documentElement.classList.toggle('dark', theme === 'dark')

if (scene === 'settings-custom') {
  // A user-recorded chord on the left sidebar plus a cleared session toggle,
  // so the shot shows both non-default states (custom + "Not set").
  localStorage.setItem(
    PANEL_TOGGLE_SHORTCUTS_KEY,
    JSON.stringify({ 'left-sidebar': { key: 's', mod: true, shift: true }, 'session-panel': null }),
  )
}

await initI18n()

// `staleTime: Infinity` + no retry: the harness has no gateway to answer any
// query a child component fires; a failed refetch must not replace the scene.
const qc = new QueryClient({
  defaultOptions: { queries: { retry: false, staleTime: Infinity, refetchOnMount: false } },
})

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <div className="bg-bg text-text min-h-screen p-8" data-capture-root>
          {scene === 'modal'
            ? <ShortcutsModal onClose={() => {}} />
            : (
                /* Width mirrors the Settings content column so wrapping matches production. */
                <div className="max-w-[760px]"><ShortcutsPanel /></div>
              )}
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
