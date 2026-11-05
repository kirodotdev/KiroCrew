/** The REAL agent import flow, with only the onboarding import calls replaced
 *  by example data (#29075): the scan reports one skip of each kind, the first
 *  apply lands with two conflicts, and every later apply (a resolution) fails
 *  after a short wait.
 *  The shoot script clicks through to the review and results steps. */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { api } from '../src/api/client'
import type { AgentImportApplyResponse, AgentImportScanResponse } from '../src/api/client'
import AgentImportFlow from '../src/components/AgentImportFlow'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const mode = params.get('theme') === 'light' ? 'light' : 'dark'
localStorage.setItem('mc-theme', mode)
localStorage.setItem('mc-color-theme', 'kiro')
document.documentElement.setAttribute('data-theme', mode === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('en')

const scan: AgentImportScanResponse = {
  sources: [{
    id: 'claude_code',
    name: 'Claude Code',
    detected: true,
    detail: '~/.claude',
    categories: [
      { id: 'skills', label: 'Skills', count: 4 },
      { id: 'mcp_servers', label: 'MCP servers', count: 2 },
    ],
  }],
  skipped: [
    { source: 'Claude Code', category: 'Memories', reason: 'row_count_limit', count: 3 },
    { source: 'Claude Code', category: 'Schedules', reason: 'unsupported_schedule_schema' },
    { source: 'Claude Code', category: 'MCP servers', reason: 'credential_bearing_server' },
    { source: 'Claude Code', category: 'Rules', reason: 'read_failed' },
    { source: 'Claude Code', category: 'Settings', reason: 'invalid_config' },
  ],
  merge_only: true,
}
const conflicted: AgentImportApplyResponse = {
  ok: true,
  conflict_strategy: 'skip',
  summary: { imported: 4, deduplicated: 1, skipped: 1, conflicts: 2, resolvable_conflicts: 2 },
}

let applies = 0
api.onboardingImportScan = async () => scan
api.onboardingImportState = async () => ({ ok: true })
api.onboardingImportApply = async () => {
  applies += 1
  if (applies === 1) return conflicted
  // Slow enough for the shoot script to photograph the running retry.
  await new Promise(resolve => setTimeout(resolve, 2500))
  throw new Error('Import service unavailable')
}
api.themeBoot = async () => ({ mode, color: 'kiro' }) as Awaited<ReturnType<typeof api.themeBoot>>
api.themes = async () => ({ themes: [] })

const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <ThemeProvider>
        <MemoryRouter initialEntries={['/']}>
          <div data-capture-root className="h-screen bg-bg text-text">
            <AgentImportFlow initialOpen onComplete={() => {}} />
          </div>
        </MemoryRouter>
      </ThemeProvider>
    </QueryClientProvider>
  </Provider>,
)
