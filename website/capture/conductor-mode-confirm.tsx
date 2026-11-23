/** Conductor mode on a chat that already has messages, through the REAL code.
 *  `?scene=confirm`: the REAL `useCrewModeSwitch` turns it on mid-chat and the
 *  REAL `AppConfirmHost` draws the dialog it asks. `?scene=blocked`: the REAL
 *  `performAgentSlotSwitch` moves a conductor mode chat, `api.chatSlotAgent`
 *  answers with the gateway's `409 crew_mode_locked`, and the REAL banner shows
 *  `agentSwitchFailureNotice` for it. Only the store rows and that one answer
 *  are mocked. Query: ?scene=confirm|blocked&lang=en|de */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { store, useAppSelector } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import { ApiError } from '../src/api/apiError'
import { sseSlots } from '../src/store/dashboardSlice'
import { appendMessage, setAgentSwitchNotice, setActiveSlot } from '../src/store/chatSlice'
import { setCrewModeCatalog } from '../src/lib/crewMode'
import { performAgentSlotSwitch } from '../src/lib/agentSwitch'
import { agentSwitchFailureNotice } from '../src/utils/agentSwitchFeedback'
import { useCrewModeSwitch } from '../src/pages/chat/page/crewModeSwitch'
import { CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY } from '../src/pages/chat/page/crewModeMidChatConfirm'
import AppConfirmHost from '../src/components/AppConfirmHost'
import AgentSwitchNoticeBanner from '../src/components/AgentSwitchNoticeBanner'
import type { ChatSlot } from '../src/types'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const mode = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') === 'blocked' ? 'blocked' : 'confirm'
localStorage.setItem('mc-theme', mode)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', mode === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') === 'de' ? 'de' : 'en')

api.themes = async () => ({ themes: [] })
api.chatSlotAgent = async () => {
  const error = 'this chat is in conductor mode, which stays on its own agent'
  throw new ApiError(409, error, JSON.stringify({ error, code: 'crew_mode_locked' }))
}
// A fresh profile: the dialog's "Don't show this notice again" was never saved.
localStorage.removeItem(CREW_MODE_MIDCHAT_CONFIRM_DISMISSED_KEY)
setCrewModeCatalog('kirocrew-conductor', ['kirocrew-conductor'])

const KEY = 'chat-1'
const row = {
  key: KEY, title: 'Release notes', messages: 2, running: false, memory_mode: 'persistent', origin: 'user',
  agent: scene === 'blocked' ? 'kirocrew-conductor' : 'kirocrew',
  agent_kind: 'template', crew_mode_root: scene === 'blocked',
} as unknown as ChatSlot
store.dispatch(sseSlots([row]))
store.dispatch(setActiveSlot(KEY))
store.dispatch(appendMessage({ role: 'user', content: 'Draft the release notes.' }))

function ConfirmScene() {
  const { turnOnCrewMode } = useCrewModeSwitch({
    currentSlot: row, defaultAgent: 'kirocrew', hasUserMessage: true,
    switchAgent: async () => true, openView: () => {}, autoOpenBoard: false,
  })
  useEffect(() => { turnOnCrewMode() }, [turnOnCrewMode])
  return null
}

function BlockedScene() {
  useEffect(() => {
    performAgentSlotSwitch(KEY, 'kirocrew', store.dispatch, 'template')
      .catch(error => store.dispatch(setAgentSwitchNotice(agentSwitchFailureNotice(error, 'kirocrew'))))
  }, [])
  return null
}

function Scene() {
  const notice = useAppSelector(s => s.chat.agentSwitchNotice)
  return (
    <div data-scene="page" data-settled={notice ? 'notice' : 'none'} className="bg-bg text-text" style={{ height: '100vh' }}>
      {/* Before the scene: effects run in tree order, so the host is registered
          when the scene's effect asks, as App's host is in the dashboard. */}
      <AppConfirmHost />
      {scene === 'confirm' ? <ConfirmScene /> : <BlockedScene />}
      {notice && <AgentSwitchNoticeBanner notice={notice} />}
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/chat']}>
          <Scene />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
