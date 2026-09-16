/** Real pages and CSS, with only gateway reads replaced by public example data. */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import { ApiError } from '../src/api/apiError'
import DiscoverPage from '../src/pages/apps/DiscoverPage'
import AppDetailPage from '../src/pages/AppDetailPage'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const mode = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', mode)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', mode === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('en')

const catalog = [
  { name: 'build-tools', displayName: 'Build Tools', description: 'Check your code and build your project.', tags: ['git'], _registry: 'team', provenance: 'external' },
  { name: 'team-notes', displayName: 'Team Notes', description: 'Keep your project notes in one place.', tags: ['writing'], _registry: 'team', provenance: 'external' },
  { name: 'community-calendar', displayName: 'Community Calendar', description: 'Plan your week with a shared calendar.', tags: ['productivity'], _registry: 'community', provenance: 'external' },
  { name: 'core-planner', displayName: 'Core Planner', description: 'Track the next steps for your work.', tags: ['productivity'], provenance: 'official' },
  { name: 'builtin-notes', displayName: 'Builtin Notes', description: 'Quick notes that stay with your workspace.', tags: ['writing'], origin: 'builtin', provenance: 'builtin' },
].map(a => ({ ...a, version: '1.0.0', author: 'Example team', installed: false }))
api.themeBoot = async () => ({ mode, color: 'kiro' }) as Awaited<ReturnType<typeof api.themeBoot>>
api.themes = async () => ({ themes: [] })
api.listApps = async () => []
api.listRegistry = async () => ({
  apps: catalog,
  editorialSections: [
    { form: 'full', items: [{ type: 'app', appRefs: ['core-planner'] }] },
    { form: 'full', items: [{ type: 'collection', title: 'Team toolkit', appRefs: ['build-tools', 'community-calendar'] }] },
  ],
})
api.listRegistries = async () => {
  if (params.has('sourceError')) throw new Error('Source metadata unavailable')
  return {
    pinned: [
      { name: 'team', label: 'Team Apps Registry', review: 'curated', repo: 'https://example.com/team.git', branch: 'main' },
      { name: 'community', label: 'Community Apps Registry', review: 'community', repo: 'https://example.com/community.git', branch: 'main' },
    ],
    registries: [{ name: 'empty', label: 'Empty Registry', repo: 'https://example.com/empty.git', branch: 'main' }],
  }
}
api.getApp = async name => {
  if (name === 'local-draft' || name === 'unlisted-app') {
    return {
      name, displayName: name === 'local-draft' ? 'Local Draft' : 'Unlisted App',
      version: '1.0.0', enabled: false, origin: name === 'local-draft' ? 'local' : 'registry',
      installedAt: '2026-01-01T00:00:00Z',
      manifest: { name, displayName: name, version: '1.0.0', author: 'Example team', description: 'An installed app not listed in the current catalog.' },
    } as Awaited<ReturnType<typeof api.getApp>>
  }
  throw new ApiError(404, 'Not installed')
}
api.system = async () => ({ hostname: '' }) as Awaited<ReturnType<typeof api.system>>

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={[params.has('detail') ? `/apps/detail/${params.get('detail')}` : '/apps']}>
          <div className="h-screen flex flex-col bg-bg text-text">
            <Routes>
              <Route path="/apps" element={<DiscoverPage />} />
              <Route path="/apps/detail/:name" element={<AppDetailPage />} />
            </Routes>
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
