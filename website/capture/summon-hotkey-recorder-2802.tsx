/**
 * Isolated capture entry for the summon-hotkey recorder (#2802): the "Desktop
 * App" card in Settings → Shortcuts, and the summon row in the shortcuts
 * reference dialog.
 *
 * WHY ISOLATED: both surfaces render only inside the desktop shell, which needs
 * `window.kirocrew.isElectron` (src/lib/electron.ts) and the preload's
 * `window.electronAPI.getGlobalHotkey` / `setGlobalHotkey` (electron/preload.js).
 * A browser on the real dashboard has neither, so the section is absent there.
 * The bridge is stubbed in the HTML head (summon-hotkey-recorder-2802.html),
 * before any app module loads; everything else is real: the real
 * ShortcutsPanel and ShortcutsModal, the real stylesheet and theme tokens, and
 * the payload shape `global-hotkey:get` and `global-hotkey:set` resolve
 * (electron/ipc-registrar.js).
 *
 * Query string: ?surface=settings|modal&scene=off|bound|fallback|remote|refused&theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does: importing the module only
// DEFINES initI18n, and without calling it every label renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { ShortcutsPanel } from '../src/pages/settings/ShortcutsPanel'
import ShortcutsModal from '../src/components/ShortcutsModal'
import '../src/index.css'

initI18n('en')

// The theme is set in the HTML head, where the theme provider reads it from.
const modal = new URLSearchParams(location.search).get('surface') === 'modal'

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/settings']}>
          <div
            style={{ background: 'var(--bg)', color: 'var(--text)', padding: 24, minHeight: '100vh' }}
            data-capture-root
          >
            {modal ? <ShortcutsModal onClose={() => {}} /> : <ShortcutsPanel />}
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
