/**
 * Isolated capture entry for the Projects (thin Project bundles) page.
 *
 * Mounts the REAL ProjectBundlesPage against the real stylesheet, theme
 * tokens and live i18n catalog, inside the same provider stack the page uses
 * in production (Redux store, react-query, a router). API responses come from
 * the capture script's route interception (gateway-free); this entry forces
 * no component state, so a frame documents the shipped wiring.
 *
 * Scenes via query string:
 *   ?theme=dark|light   theme tokens on <html data-theme>.
 *   ?route=<path>       the router's initial entry (default /project-bundles),
 *                       so a frame can arrive at a Project detail by deep link
 *                       (/project-bundles?project=<id>).
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ProjectBundlesPage from '../src/pages/ProjectBundlesPage'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const route = params.get('route') || '/project-bundles'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={[route]}>
          <div className="h-screen flex flex-col bg-bg text-text" data-capture-root>
            <ProjectBundlesPage />
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
