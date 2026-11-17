/** A refused artifact folder move, through the REAL `useMoveArtifactToFolder`
 *  and the REAL shell banner (#34554). Only `api.setArtifactFolder` is replaced:
 *  it refuses with a 403 and the gateway's reason, as a move into another Bot's
 *  folder does. The entry fires one move on load; the shoot script photographs
 *  what the user then sees. Query string: ?theme=light|dark&lang=en|de */
import { useEffect } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store, useAppSelector } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import { ApiError } from '../src/api/apiError'
import { useMoveArtifactToFolder } from '../src/hooks/useArtifactFolders'
import AgentSwitchNoticeBanner from '../src/components/AgentSwitchNoticeBanner'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const mode = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', mode)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', mode === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n(params.get('lang') === 'de' ? 'de' : 'en')

api.themes = async () => ({ themes: [] })
api.setArtifactFolder = async () => {
  throw new ApiError(403, "that folder belongs to another Bot's library")
}

function Scene() {
  const move = useMoveArtifactToFolder()
  const notice = useAppSelector(s => s.chat.agentSwitchNotice)
  useEffect(() => { move('q4-report', 'archive') }, [move])
  return (
    <div data-scene="page" data-settled={notice ? 'notice' : 'none'} className="bg-bg text-text" style={{ height: '100vh' }}>
      {notice && <AgentSwitchNoticeBanner notice={notice} />}
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/artifacts']}>
          <Scene />
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
