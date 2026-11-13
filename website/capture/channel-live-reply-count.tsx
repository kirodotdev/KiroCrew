/**
 * Capture entry for a thread reply that arrives live (#33306).
 *
 * Mounts the REAL ChannelPage with only the api reads stubbed: one channel
 * holding one top-level message with no replies yet. The capture script then
 * dispatches the same `kirocrew-channel` window event the app shell forwards
 * from the socket, carrying a reply in that message's thread, and shapes the
 * payload exactly as `Channel.post` broadcasts it (the reply only; the
 * parent's raised reply_count is not re-sent).
 *
 *   ?theme=dark|light
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

const theme = new URLSearchParams(location.search).get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme)

const CHANNEL = {
  id: 'ch1',
  topic: 'Gamma rollout',
  members: {
    a1: {
      id: 'a1', role: 'Researcher', agent_name: 'kirocrew', state: 'listening',
      listen_mode: 'mention', approval_policy: 'writes', session_key: 'k1',
    },
  },
  messages: [
    {
      id: 'p1', from_id: 'human', from_role: 'You', content: 'Why did the gamma deploy fail?',
      msg_type: 'broadcast', timestamp: 1_790_000_000, reply_count: 0,
    },
  ],
}

api.channelsList = async () => ({ channels: [CHANNEL] }) as Awaited<ReturnType<typeof api.channelsList>>
api.channelGet = async () => CHANNEL as Awaited<ReturnType<typeof api.channelGet>>
api.channelPresets = async () => ({ presets: [] }) as Awaited<ReturnType<typeof api.channelPresets>>
api.themeBoot = async () => ({ mode: theme, color: 'kiro' }) as Awaited<ReturnType<typeof api.themeBoot>>

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter initialEntries={['/channels']}>
        <div
          style={{ background: 'var(--bg)', color: 'var(--text)', height: 420, display: 'flex', flexDirection: 'column' }}
          data-capture-root
        >
          <ChannelPage />
        </div>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
