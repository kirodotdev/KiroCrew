/**
 * Evidence for the paste-block sidecar on ChatPane and SideChat (#11337).
 *
 * THE CHANGE: both hosts now pass `pasteBlocks` / `onPasteBlocksChange` to the
 * REAL native composer, so a large paste collapses into a `[ Paste #N · M
 * lines ]` token there exactly as it does in the main chat, and the token is
 * expanded for the model at send. Before, the same paste stayed raw text on
 * these two surfaces.
 *
 * Scenes mount the REAL host against the real store, stylesheet, theme tokens
 * and live i18n catalog. API responses come from the capture script's route
 * interception (gateway-free). The script performs the paste itself through a
 * clipboard event on the composer, so what the frame shows is the shipped
 * paste handler reacting, not seeded state.
 *
 * ?host=pane|side  ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import ChatPane from '../src/components/ChatPane'
import SideChat from '../src/pages/chat/SideChat'
import { initI18n } from '../src/i18n/all'
import { store } from '../src/store'
import { sseConnected, sseSlots } from '../src/store/dashboardSlice'
import { hydrateSlotMessages, sseSideResult } from '../src/store/chatSlice'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const host = params.get('host') === 'side' ? 'side' : 'pane'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.dataset.mode = theme
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const SLOT = 'capture-paste-slot'

// The composer blocks sends while the gateway reads as offline; these frames
// show the connected state (SideChat reads it through useConnected).
store.dispatch(sseConnected())
store.dispatch(
  sseSlots([
    { key: SLOT, title: 'release notes', messages: 2, running: false, mode: 'member', agent: 'writer' },
  ] as never),
)
// A warm (non-active) slot, the shape a member DM thread or a split pane has.
store.dispatch(
  hydrateSlotMessages({
    slot: SLOT,
    messages: [
      { role: 'user', content: 'Can you turn my raw notes into release notes?', ts: '2026-09-22T00:00:00Z' },
      { role: 'assistant', content: 'Sure — paste the notes here and I will structure them.', ts: '2026-09-22T00:00:05Z' },
    ],
    hasMore: false,
    total: 2,
    running: false,
  } as never),
)
if (host === 'side') {
  // A settled side exchange (the same frames the WS delivers), so the panel
  // is idle: the send under test starts a turn and mints its own bubble.
  store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r-seed', role: 'user', content: 'what does the stack trace in my log mean?', ts: 1758499210 }))
  store.dispatch(sseSideResult({ slot: SLOT, run_id: 'r-seed', role: 'assistant', content: 'It is a retry loop hitting a closed socket — paste the log lines and I will point at the first failing step.', ts: 1758499215, final: true }))
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

async function main() {
  await initI18n()
  createRoot(document.getElementById('root')!).render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter>
          {host === 'pane' ? (
            <div className="h-screen bg-bg text-text" data-capture-root>
              <ChatPane slotKey={SLOT} frameless />
            </div>
          ) : (
            <div
              data-capture-root
              style={{ width: 460, height: 560, margin: '0 auto', background: 'var(--bg)', border: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}
            >
              <SideChat slot={SLOT} />
            </div>
          )}
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
}

main()
