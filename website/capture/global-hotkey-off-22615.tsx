/**
 * Isolated capture entry for the system-wide summon hotkey in its OFF state,
 * in Settings → Shortcuts → "Desktop App" (#22615 / PR #22616).
 *
 * WHY ISOLATED, and why this cannot be captured any other way: the "Desktop
 * App" section renders only when `useGlobalHotkey()` returns non-null, which
 * requires BOTH `window.kirocrew.isElectron` (see src/lib/electron.ts) and
 * `window.electronAPI.getGlobalHotkey` (see electron/preload.js). A browser
 * visiting the real dashboard has neither, so the whole section is absent — the
 * surface is unreachable from a plain SPA session by construction. A desktop
 * build would show it, but the OFF state is new in this PR and never ships in a
 * released app, so there is no installed build that renders it either.
 *
 * The desktop bridge is stubbed in the HTML head (global-hotkey-off-22615.html)
 * — it MUST be set before any app module loads because src/lib/electron.ts
 * reads `window.kirocrew` at import time. Everything else is real: the REAL
 * ShortcutsPanel, the REAL stylesheet and theme tokens, and the same
 * `{accelerator, default}` payload shape `global-hotkey:get` returns (see
 * electron/ipc-registrar.js). The one field under test is `accelerator: ""`,
 * which is exactly what the main process reports when the summon hotkey is off
 * — whether by the fresh-install opt-out seed or because a stored chord and the
 * platform default both failed to bind.
 *
 * Scene + theme come from the query string: ?scene=off&theme=dark
 *   scene=off (default) — accelerator "" (the Off row + how-to-enable hint)
 *   scene=on            — accelerator bound (the control case, for contrast)
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only
// DEFINES initI18n, and without calling it every label renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { ShortcutsPanel } from '../src/pages/settings/ShortcutsPanel'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/settings']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24 }}
          data-capture-root
        >
          <ShortcutsPanel />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
