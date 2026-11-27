/**
 * Capture entry: a model list that kiro-cli's own sign-in holds back (`/api/models`
 * answers 503 `kiro_prerequisite_required`), with every API answered locally.
 *
 * - ?scenario=picker    the composer's REAL ModelEffortDropdown with the sign-in row
 * - ?scenario=confirm   the Run in terminal confirm that the row's button opens
 * - ?scenario=settings  the REAL Settings ▸ Chat panel's model-list banner
 * ?theme=dark|light  ?lang=en|de
 */
import { createRef, useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import { ApiError } from '../src/api/apiError'
import { initI18n } from '../src/i18n/all'
import ModelEffortDropdown from '../src/components/ModelEffortDropdown'
import { useKiroCliSignInInTerminal } from '../src/components/KiroCliSignInInTerminal'
import { ChatPanel } from '../src/pages/settings/ChatPanel'
import { setTerminalEnabledFlag } from '../src/utils/terminalRegistry'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const mode = params.get('theme') === 'light' ? 'light' : 'dark'
const scenario = params.get('scenario') || 'picker'
setTerminalEnabledFlag(params.get('canrun') !== '0')
localStorage.setItem('mc-theme', mode)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', mode === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') === 'de' ? 'de' : 'en')

const signedOut = () => Promise.reject(new ApiError(
  503,
  'Kiro CLI is not signed in',
  JSON.stringify({ error: 'Kiro CLI is not signed in', code: 'kiro_prerequisite_required' }),
))
const loose = api as unknown as Record<string, unknown>
loose.themes = async () => ({ themes: [] })
loose.models = signedOut
loose.dashboardConfig = async () => ({ verbosity: 'default', default_memory_mode: 'persistent' })
loose.updateDashboardConfig = async () => ({})
loose.kirocrewConfig = async () => ({ agent: { model: 'auto', reasoning_effort: '' }, session: {}, dashboard: {}, knowledge: {} })
loose.kirocrewAgents = async () => ({ agents: [], default_agent: 'default' })
loose.agentResolvedModel = async () => ({ model: '', pinned: false })
loose.patchConfig = async () => ({})
loose.voiceConfig = async () => ({ enabled: false })
loose.sttConfig = async () => ({ enabled: false, models: {}, language_codes: [] })
loose.tipsStatus = async () => ({ enabled_config: true, opted_out: false })
loose.featureVideoStatus = async () => ({ enabled: true, download_enabled: false, release: 'r1', cached: 0, total: 0, downloading: null })

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
// The status the welcome screen reads at boot: the command the button runs.
qc.setQueryData(['kiro-prerequisite'], { login_command: 'kiro-cli login' })

const noop = () => {}

function Picker() {
  const anchor = { right: 380, top: 330, left: 40, bottom: 350, width: 340, height: 20 } as DOMRect
  return (
    <ModelEffortDropdown
      anchorRect={anchor}
      dropdownRef={createRef()}
      inputRef={createRef()}
      models={[{ name: 'auto', description: 'Models chosen by task for optimal usage and consistent quality' }]}
      activeModel="auto"
      onSelectModel={noop}
      filter=""
      setFilter={noop}
      onClose={noop}
      hasEffort={false}
      slot={null}
      currentEffort=""
      onListKeyDown={noop}
      kiroCliSignIn={{ run: noop, command: 'kiro-cli login', canRun: params.get('canrun') !== '0' }}
    />
  )
}

function Confirm() {
  const { askToSignIn, dialog } = useKiroCliSignInInTerminal()
  useEffect(() => { askToSignIn() }, [askToSignIn])
  return <>{dialog}</>
}

function Scene() {
  return (
    <div data-scene="page" className="bg-bg text-text" style={{ minHeight: '100vh', padding: 16 }}>
      {scenario === 'settings' ? <ChatPanel /> : scenario === 'confirm' ? <Confirm /> : <Picker />}
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/settings?tab=chat&sub=models']}>
          <Scene />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
