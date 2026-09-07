/**
 * Isolated capture entry for the Spec Builder #7662 fixes.
 *
 * Mounts the REAL components against the real stylesheet, theme tokens and
 * live i18n catalog; API responses come from the capture script's route
 * interception (gateway-free). Two scenes, one per fixed surface:
 *
 *   ?scene=detail — SpecDetail; the script opens the delete confirm and the
 *                   intercepted DELETE refuses, so the frame documents the
 *                   failure rendering INSIDE the dialog. `setErr` calls are
 *                   recorded on `window.__setErrCalls` so the script can
 *                   assert the occluded page-top banner is no longer used.
 *   ?scene=rail   — SpecRail; the script types a filter that matches nothing
 *                   and photographs the empty state's Clear-filter exit.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import SpecDetail from '../src/apps/spec-builder/components/SpecDetail'
import SpecRail from '../src/apps/spec-builder/components/SpecRail'
import type { SpecSummary } from '../src/apps/spec-builder/api'
import { AppApiProvider } from '../src/app-sdk'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'detail'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

declare global {
  interface Window { __setErrCalls: string[] }
}
window.__setErrCalls = []

const SPECS: SpecSummary[] = [
  { name: 'checkout-flow', title: 'Checkout flow', phase: 'design', running: true, status: 'planning' },
  { name: 'dark-mode', title: 'Dark mode', phase: 'tasks', running: false, status: 'planning' },
  { name: 'billing-export', title: 'Billing export', phase: 'requirements', running: false, status: 'planning' },
] as SpecSummary[]

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="h-screen flex bg-bg text-text" data-capture-root>
            {scene === 'rail'
              ? (
                <SpecRail
                  specs={SPECS}
                  sel={null}
                  setSel={() => {}}
                  onNew={() => {}}
                  onSettings={() => {}}
                  width={280}
                />
              )
              : (
                <AppApiProvider
                  appName="spec-builder"
                  allowedApiPaths={['/api/chat']}
                  allowedEvents={[]}
                  subscribeFn={() => () => {}}
                  navigateFn={() => {}}
                  notifyFn={() => {}}
                >
                  <SpecDetail
                    name="checkout-flow"
                    setErr={(m) => { window.__setErrCalls.push(m) }}
                  />
                </AppApiProvider>
              )}
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
