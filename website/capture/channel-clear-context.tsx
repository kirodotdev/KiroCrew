/**
 * Isolated capture entry for the clear-context states this change adds.
 *
 * WHY ISOLATED: every state here is a property of a SERVER RESPONSE to a
 * destructive request. Reaching them in a real session means clearing a real
 * channel's context and deleting its transcript, so the response is stubbed and
 * everything else is real: the REAL ChannelPage, the REAL Modal and ErrorNotice,
 * the REAL stylesheet and theme tokens, and the same payload shapes the gateway
 * actually returns from /channels and /clear-context.
 *
 * Scenes (?scene=), driven to the pictured state by the capture script:
 *   confirm  the clear-all dialog open, whose danger button restates the act
 *   done     a clean clear acknowledged in the page and on the clicked row
 *   busy     the request in flight, header opener visibly disabled
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

// Importing the i18n module only DEFINES initI18n; without calling it every
// label in the frame renders blank.
import { initI18n } from '../src/i18n'
import { store } from '../src/store'
import { api } from '../src/api/client'
import ChannelPage from '../src/pages/ChannelPage'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'confirm'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

/** Two members, so a per-agent mark is legible as belonging to ONE row. */
const CHANNEL = {
  id: 'ch1',
  topic: 'Release notes for 0.5.0',
  members: {
    a1: {
      id: 'a1', role: 'Researcher', agent_name: 'kirocrew', state: 'listening',
      listen_mode: 'mention', approval_policy: 'writes', session_key: 'k1',
    },
    a2: {
      id: 'a2', role: 'Scribe', agent_name: 'kirocrew', state: 'listening',
      listen_mode: 'mention', approval_policy: 'writes', session_key: 'k2',
    },
  },
  messages: [],
}

type Clear = Awaited<ReturnType<typeof api.channelClearContext>>

api.channelsList = async () => ({ channels: [CHANNEL] }) as Awaited<ReturnType<typeof api.channelsList>>
api.channelGet = async () => CHANNEL as Awaited<ReturnType<typeof api.channelGet>>
api.channelPresets = async () => ({ presets: [] }) as Awaited<ReturnType<typeof api.channelPresets>>
api.themeBoot = async () => ({ mode: theme, color: 'kiro' }) as Awaited<ReturnType<typeof api.themeBoot>>
api.channelClearContext = scene === 'busy'
  // Never settles: the busy state IS the request still being open.
  ? () => new Promise<Clear>(() => {})
  : async () => ({ ok: true, cleared: ['Researcher'] }) as Clear

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/channels']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', height: 560, display: 'flex', flexDirection: 'column' }}
          data-capture-root
        >
          <ChannelPage />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
