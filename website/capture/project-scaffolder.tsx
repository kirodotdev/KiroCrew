/**
 * Evidence for the Create Folders From Project app.
 *
 * Mounts the REAL `ProjectScaffolderPage` against the real stylesheet, theme
 * tokens and live i18n catalog, with only `fetch` stubbed to answer the two
 * scaffold endpoints (and the picker's directory listings) from a synthetic
 * monorepo — so no real project names reach a frame, and every string in the
 * frame is the string that ships. Nothing here re-implements the page.
 *
 *   ?scan=ok            a populated preview (default)
 *   ?scan=empty         no sub-projects found
 *   ?scan=refused       the root is refused (400 with the folder API's own prose)
 *   ?scan=ok-then-fail  first scan succeeds, the re-scan is refused (400)
 *   ?scan=ok-then-slow  first scan succeeds, the re-scan never answers (in-flight frame)
 *   ?scan=ok-newroot    a populated preview whose root has no folder yet
 *   ?create=ok          create succeeds with one refused folder (default)
 *   ?create=stale       create is refused as stale (the tree moved)
 *   ?create=refused     create is refused outright (rate limit; retry is the right action)
 *   ?create=slow        create never answers (in-flight frame)
 *   ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import ProjectScaffolderPage from '../src/apps/project-scaffolder/ProjectScaffolderPage'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scanMode = params.get('scan') ?? 'ok'
const createMode = params.get('create') ?? 'ok'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

export const ROOT = '/work/acme-shop'

const SCAN = {
  root: ROOT,
  root_existing: true,
  status: 'ok',
  candidates: [
    { path: `${ROOT}/apps`, name: 'apps', parent_path: null, tier: 'auto', signals: ['manifest:package.json'], existing: false, selected: true },
    { path: `${ROOT}/apps/web`, name: 'web', parent_path: `${ROOT}/apps`, tier: 'auto', signals: ['manifest:vercel.json', 'member'], existing: false, selected: true },
    { path: `${ROOT}/apps/admin`, name: 'admin', parent_path: `${ROOT}/apps`, tier: 'auto', signals: ['manifest:firebase.json', 'member'], existing: false, selected: true },
    { path: `${ROOT}/services`, name: 'services', parent_path: null, tier: 'auto', signals: ['git'], existing: false, selected: true },
    { path: `${ROOT}/services/api`, name: 'api', parent_path: `${ROOT}/services`, tier: 'auto', signals: ['manifest:pyproject.toml', 'git'], existing: false, selected: true },
    { path: `${ROOT}/packages/ui`, name: 'ui', parent_path: null, tier: 'auto', signals: ['member', '.kiro'], existing: true, selected: false },
    { path: `${ROOT}/packages/config`, name: 'config', parent_path: null, tier: 'offered', signals: ['member'], existing: false, selected: false },
    { path: `${ROOT}/services/legacy`, name: 'legacy', parent_path: `${ROOT}/services`, tier: 'offered', signals: ['manifest:Makefile'], existing: false, selected: false },
    { path: `${ROOT}/tools/lint`, name: 'lint', parent_path: null, tier: 'offered', signals: ['manifest:package.json'], existing: false, selected: false },
  ],
  warnings: ['skipped unreadable directory /work/acme-shop/vendor/private: Permission denied'],
}

const EMPTY_SCAN = { root: ROOT, root_existing: false, status: 'empty', candidates: [], warnings: [] }

const CREATE_OK = {
  root: ROOT,
  // The root already had a folder (SCAN.root_existing), so it is the one
  // skipped path -- the only skip the server produces without a race. The
  // tally reconciles: root + 5 selected = 4 created + 1 already existed + 1 failed.
  created: [
    { path: `${ROOT}/apps`, folder_id: 'f1', name: 'apps' },
    { path: `${ROOT}/apps/web`, folder_id: 'f2', name: 'web' },
    { path: `${ROOT}/services`, folder_id: 'f3', name: 'services' },
    { path: `${ROOT}/services/api`, folder_id: 'f4', name: 'api' },
  ],
  skipped_existing: [ROOT],
  failed: [
    { path: `${ROOT}/apps/admin`, error: 'That directory was moved or replaced after the scan — re-scan and retry', code: 'folder_project_dir_moved' },
  ],
  warnings: [],
}

let scanCalls = 0
const json = (status: number, body: unknown): Response =>
  ({ ok: status < 400, status, json: async () => body, text: async () => JSON.stringify(body) }) as Response

window.fetch = async (input: RequestInfo | URL) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/recent-projects')) return json(200, { dirs: [ROOT, '/work/other'] })
  if (url.startsWith('/api/browse-dirs')) return json(200, { path: '/work', parent: '/', dirs: [{ name: 'acme-shop', path: ROOT }] })
  if (url.startsWith('/api/project-scaffold/scan')) {
    scanCalls += 1
    if (scanMode === 'empty') return json(200, EMPTY_SCAN)
    if (scanMode === 'refused') return json(400, { error: 'Project directory must be an existing directory', code: 'folder_scan_root_invalid' })
    if (scanMode === 'ok-then-fail' && scanCalls > 1) return json(400, { error: 'Project directory must be an existing directory', code: 'folder_scan_root_invalid' })
    // The root has no folder yet: the counter promises it ("Root folder + N selected").
    if (scanMode === 'ok-newroot') return json(200, { ...SCAN, root_existing: false })
    // Never resolves: holds the page in its in-flight state for the capture.
    if (scanMode === 'ok-then-slow' && scanCalls > 1) return new Promise<Response>(() => {})
    return json(200, SCAN)
  }
  if (url.startsWith('/api/project-scaffold/create')) {
    if (createMode === 'refused') {
      // A whole-call refusal that is NOT the tree moving (that one routes to the
      // stale banner): the server's own rate-limit refusal, where retrying is
      // exactly the right thing, so Create stays enabled beside the notice.
      return json(429, { error: 'too many folders created recently; retry shortly', code: 'create_rate_limited' })
    }
    // Never resolves: holds the page in its create-in-flight state for the capture.
    if (createMode === 'slow') return new Promise<Response>(() => {})
    if (createMode === 'stale') {
      return json(400, {
        error: 'selection is out of date — re-scan before creating folders',
        code: 'folder_scaffold_selection_stale',
        unknown: [`${ROOT}/services/api`],
      })
    }
    return json(200, CREATE_OK)
  }
  return json(404, { error: 'not stubbed' })
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

const root = createRoot(document.getElementById('root')!)
root.render(
  <QueryClientProvider client={qc}>
    <div data-capture-root style={{ width: '100%', maxWidth: 900, margin: '0 auto', background: 'var(--bg)', minHeight: 480 }}>
      <ProjectScaffolderPage />
    </div>
  </QueryClientProvider>,
)
