/** The app detail page's "Dashboard access" card, through the REAL component.
 *  Only the three `api` calls it makes are answered here (an offer from a registry
 *  install; the grant; the restart). `?scene=needs` is an app whose last token
 *  request was refused for want of the grant; `?scene=unsupported` is a host whose
 *  sandbox cannot bind the access. The shooter clicks through the grant itself.
 *  Query: ?scene=needs|unsupported&theme=dark|light */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import AppDetailDashboardAccess from '../src/pages/AppDetailDashboardAccess'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const mode = params.get('theme') === 'light' ? 'light' : 'dark'
const scene = params.get('scene') === 'unsupported' ? 'unsupported' : 'needs'
localStorage.setItem('mc-theme', mode)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', mode === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('en')

const OFFER = {
  app: 'crew-tunnel',
  registry: 'crew-tunnels',
  source: 'https://example.invalid/crew-tunnel.git',
  version: '0.9.4',
  text: 'Allow crew-tunnel 0.9.4 (registry crew-tunnels, source https://example.invalid/crew-tunnel.git) to control this dashboard? It gets full dashboard control, including running agents, except credential and admin routes. Terminal and approvals are local-only, and admin is done locally.',
}
let granted = false
const view = () => ({
  granted,
  offer: scene === 'unsupported' ? null : OFFER,
  platform_supported: scene !== 'unsupported',
  needs_access: !granted,
})
api.getAppDashboardAccess = async () => view()
api.grantAppDashboardAccess = async () => {
  granted = true
  return { ...view(), restart_required: true }
}
api.restartAppForDashboardAccess = async () => ({ ...view(), restarted: true })

createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={new QueryClient()}>
    <ThemeProvider>
      <div data-scene="page" style={{ padding: 24, maxWidth: 640, background: 'var(--bg)', color: 'var(--text)' }}>
        <AppDetailDashboardAccess appName="crew-tunnel" />
      </div>
    </ThemeProvider>
  </QueryClientProvider>,
)
