/**
 * Isolated capture entry for #25970: cut-off lines in the Files tab header, the
 * folder panel and the remote git history show their full text in the shared
 * bubble on keyboard focus, instead of only in a native `title`.
 *
 * Mounts the REAL panels at a narrow dock width with only the transport stubbed.
 * Query string: ?theme=dark&view=files|folder|git
 */
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { BrandingProvider } from '../src/hooks/useBranding'
import FilesHomePanel from '../src/pages/chat/FilesHomePanel'
import FolderPanel from '../src/pages/chat/FolderPanel'
import RemoteGitPanel from '../src/pages/chat/RemoteGitPanel'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const view = params.get('view') || 'files'
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const DIR = '/Users/me/workspace/customer-onboarding-service-migration-2026'
const SUBJECT = 'fix(billing): keep the invoice total and the tax line in step when a refund lands mid-cycle'

const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  let body: unknown = {}
  if (url.includes('/api/dashboard/branding')) body = { bot_name: 'Kiro Crew', avatar: '/logo.png', direct_local: true }
  else if (url.includes('/api/project/tree')) body = { root: DIR, paths: ['README.md', 'src/main.py'], directories: ['src'], repo: true }
  else if (url.includes('/api/project/git/status')) body = { repo: true, branch: 'main', ahead: 0, behind: 0, files: [] }
  else if (url.includes('/api/browse-files')) {
    body = { path: DIR, parent: '/Users/me/workspace', dirs: [{ name: 'src', path: `${DIR}/src`, mtime: 1 }], files: [{ name: 'README.md', path: `${DIR}/README.md`, mtime: 2 }] }
  } else if (url.includes('op=git_status')) body = { root: '/w', git: true, output: '## main...origin/main\0' }
  else if (url.includes('op=git_log')) body = { root: '/w', git: true, output: `a1b2c3d\tme\t1700000000\t${SUBJECT}\ne4f5a6b\tme\t1699990000\tdocs: note the refund window\n` }
  return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } }))
}

function Harness() {
  return (
    <div style={{ width: 300, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }} className="bg-bg text-text">
      {view === 'files' && <FilesHomePanel projectDir={DIR} onFileOpen={() => {}} />}
      {view === 'folder' && <FolderPanel path={DIR} onClose={() => {}} />}
      {view === 'git' && <RemoteGitPanel slot="chat-1" name="Remote" />}
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <BrandingProvider>
          <Harness />
        </BrandingProvider>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
