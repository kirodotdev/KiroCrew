/**
 * Isolated capture entry for a settled tool-call group stacked directly above a
 * "Sub-agents finished" recovery card, the pairing a finished turn shows in a
 * session pane.
 *
 * Mounts the REAL CollapsibleToolGroup and RecoveryCard against the real
 * stylesheet, theme tokens and i18n catalog. The two headers are disclosure rows
 * in one column, so their chevrons should sit on the same x and share one size.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import CollapsibleToolGroup from '../src/pages/chat/CollapsibleToolGroup'
import RecoveryCard, { parseRecoveryMessage } from '../src/pages/chat/RecoveryCard'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

// Parsed after i18n init: the card's title and detail are resolved at parse time.
const synthesis = parseRecoveryMessage(
  '[SYSTEM] Sub-agent synthesis: 2 sub-agents finished. Summarize their results for the user.',
)!
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <div
          data-capture-root
          style={{ width: 560, margin: '16px auto', background: 'var(--bg)', padding: 16, display: 'flex', flexDirection: 'column' }}
        >
          <CollapsibleToolGroup count={2}>
            <div>row</div>
          </CollapsibleToolGroup>
          <RecoveryCard parsed={synthesis} />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
