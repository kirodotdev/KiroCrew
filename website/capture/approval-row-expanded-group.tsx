/**
 * Isolated capture entry for the collapsible tool group's approval row while
 * the group is EXPANDED (#5487).
 *
 * WHY ISOLATED: the pending state only exists inside a live chat session with
 * an agent parked on an approval — neither exists in a capture run. This
 * mounts the REAL CollapsibleToolGroup against the real stylesheet, theme
 * tokens and live i18n catalog, feeding it the same props ChatMessageList
 * passes for a recent running group with an unresolved permission
 * (autoExpand + isRunning + hasPermission + permissionMeta). Grouped
 * permission messages render null as children in ChatMessageList, so the
 * children here are empty — exactly the shipped expanded view.
 *
 * The defect this documents: the approval row (command preview + buttons) was
 * gated on !expanded, while a pending group auto-expands — the one turn
 * waiting on the user was the one turn they could not answer. The after-frame
 * shows the row reachable while expanded.
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
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        {/* The group's real habitat is the embed's message column. canTrust is
            set because ChatEmbed — the only production mount that renders this
            row — is the trust-honoring mount (#5434). */}
        <div data-capture-root style={{ width: 560, margin: '16px auto', background: 'var(--bg)', padding: 16 }}>
          <CollapsibleToolGroup
            count={0}
            autoExpand
            isRunning
            hasPermission
            pendingPermCount={1}
            permissionMeta={{ tool_input: { command: 'git push origin feature' } }}
            onApprove={() => {}}
            canTrust
          >
            {null}
          </CollapsibleToolGroup>
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
