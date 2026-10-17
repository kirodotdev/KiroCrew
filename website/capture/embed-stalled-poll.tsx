/**
 * Isolated capture entry for a ChatEmbed whose poll keeps failing after a page
 * was shown: the header kept reading "streaming" on a stale snapshot.
 *
 * Mounts the REAL ChatEmbed against the real stylesheet, theme tokens and live
 * i18n catalog; the transcript comes from the capture script's route
 * interception (gateway-free). The script answers the first slot read with a
 * running turn and fails every later one. `?w=` sets the box width.
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatEmbed from '../src/app-sdk/ChatEmbed'
import { AppScopedApiProvider } from '../src/app-sdk/scopedApi'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          <div className="min-h-screen bg-bg text-text p-4" data-capture-root>
            <div style={{ width: Number(params.get('w') || 720), height: 520 }} data-capture-box>
              <AppScopedApiProvider
                appName="capture-app"
                allowedApiPaths={['/api/chat*']}
                allowedEvents={[]}
                navigateFn={() => {}}
              >
                <ChatEmbed slotKey="capture-app-embed" startAtBottom />
              </AppScopedApiProvider>
            </div>
          </div>
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
