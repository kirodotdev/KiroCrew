/**
 * Isolated capture entry for the trust prompt's desktop box.
 *
 * WHY ISOLATED: the box shows only for an app whose manifest declares a
 * `desktop` block, and no app in a registry declares one yet. The modal, the
 * box and the stylesheet are the real ones; the target is the one the Library
 * builds from such a manifest.
 *
 * Scenes (?scene=): spanning (an avatar across displays, a background page, a
 * panel) or shortcut (one overlay and a global shortcut).
 * Theme: &theme=dark|light. Language: &lang=<code>.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// `../src/i18n/all` registers every language catalog, as the shared entry
// contract requires of every capture.
import { initI18n } from '../src/i18n/all'
import TrustAppModal from '../src/components/appstore/TrustAppModal'
import { desktopRequestOf } from '../src/components/appstore/desktopRequest'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') || 'en')

const DESKTOPS = {
  spanning: {
    windows: [
      { id: 'pet', entry: 'pet.mjs', kind: 'overlay', displays: 'all', openOnEnable: true },
      { id: 'brain', entry: 'brain.mjs', kind: 'background', openOnEnable: true },
      { id: 'panel', entry: 'panel.mjs', kind: 'panel' },
    ],
  },
  shortcut: {
    windows: [{ id: 'pet', entry: 'pet.mjs', kind: 'overlay', openOnEnable: true }],
    shortcuts: [{ id: 'toggle', accelerator: 'CommandOrControl+Alt+P', window: 'pet' }],
  },
}
const scene = (params.get('scene') || 'spanning') as keyof typeof DESKTOPS

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={new QueryClient()}>
    <MemoryRouter>
      <TrustAppModal
        app={{
          name: 'desk-pet',
          displayName: 'Desk Pet',
          origin: 'registry',
          trustRepository: 'https://github.com/example/kirocrew-app-desk-pet',
          retryInstalls: true,
          desktop: desktopRequestOf({ desktop: DESKTOPS[scene] }),
        }}
        pending={false}
        failed={false}
        granted={false}
        onCancel={() => {}}
        onConfirm={() => {}}
      />
    </MemoryRouter>
  </QueryClientProvider>,
)
