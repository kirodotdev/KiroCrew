/**
 * Isolated capture entry for the clear-context states this change adds.
 *
 * WHY ISOLATED: every state is a property of a SERVER RESPONSE to a destructive
 * request, so reaching one for real means deleting a channel's transcript. Only
 * the response is stubbed; the ChannelPage, Modal, ErrorNotice, stylesheet and
 * payload shapes are the real ones.
 *
 * Scenes (?scene=), driven to the pictured state by the capture script:
 *   confirm   the clear-all dialog open, whose danger button restates the act
 *   done      a clean clear acknowledged in the page and on the clicked row
 *   busy      the request in flight, header opener visibly disabled
 *   row-busy  the request in flight for ONE agent, that row's control busy
 *   error     the clear refused, reported in the page's own failure notice
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
import { ApiError } from '../src/api/apiError'
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
const REFUSAL = JSON.stringify({ error: 'not found' })

api.channelClearContext = scene === 'busy' || scene === 'row-busy'
  // Never settles: the busy state IS the request still being open.
  ? () => new Promise<Clear>(() => {})
  : scene === 'error'
    // The 404 this route returns for a channel that went away between the
    // render and the request, which is the failure the page must still report.
    ? async (): Promise<Clear> => { throw new ApiError(404, REFUSAL, REFUSAL) }
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
