/**
 * Isolated capture entry for the FolderConfigModal agent picker.
 *
 * Mounts the REAL `FolderConfigModal` through the REAL SimpleSelect (Radix),
 * Modal portal and react-query wiring, so each frame photographs shipped markup
 * and shipped i18n strings. Only the transport is stubbed: one `fetch` shim
 * answers `GET /api/agents?project_path=…` from an in-page roster table (and
 * 500s / hangs for the error / loading scenes), because there is no gateway
 * behind a capture page. The component's own debounce, useQuery, orphan-flag and
 * Save-gating logic all run unchanged — the states are driven through its actual
 * props and inputs, never faked markup.
 *
 * Scenes, selected with `?state=` (mirror the six PR attachments):
 *   1 dropdown-populated   — picker OPEN, listing the folder dir's agents
 *   2 roster-loading       — re-scope in flight: Default-agent loading hint, Save disabled
 *   3 roster-loaded        — the re-scope settled: hint cleared, Save recovered
 *   4 rescope-orphan       — "repo-dev (not in this project)", notice, Create folder disabled
 *   5 scan-error           — "repo-dev (can't verify)", error+Retry row, Create folder disabled
 *   6 agent-picked         — ordinary success, a valid pick for the folder's dir
 *
 * Theme comes from the query string: ?theme=dark|light (default dark).
 *
 * The multi-step scenes (a Radix pick, a re-scope) are driven from the bash
 * orchestrator via playwright-cli real pointer events + a native-value-setter
 * eval on the project-dir input; this file only sets up the initial props and
 * the transport, and exposes `window.__capSetDir` so the orchestrator can drive
 * the controlled project-dir input through React's onChange the reliable way.
 */
import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'

import FolderConfigModal from '../src/components/FolderConfigModal'
import { initI18n } from '../src/i18n/all'
import { ChatFolder, ChatTag } from '../src/types'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const state = params.get('state') || '1'
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')
initI18n('en')

// --- Transport stub -------------------------------------------------------
// The roster the backend would return for a scanned directory. The dir name
// drives success / error / hang so the orchestrator can re-scope into any of
// them by typing a path — matching how the real component keys its query.
// Each dir's project agents. The real /api/agents unions the global roster in,
// so a global agent (kirocrew / kirocrew-dev) stays valid under any scanned dir;
// mirror that so a global seeded pick survives a re-scope while a project-only
// agent (repo-dev) orphans when the dir it belongs to is left.
const PROJECT_ROSTERS: Record<string, { name: string }[]> = {
  '/repo/pay': [{ name: 'repo-dev' }, { name: 'repo-reviewer' }],
  '/repo/a': [{ name: 'repo-dev' }],
  '/repo/ok': [{ name: 'repo-dev' }],
  '/repo/team': [{ name: 'repo-dev' }, { name: 'repo-reviewer' }],
}
function rosterFor(path: string): { name: string }[] {
  return [...(PROJECT_ROSTERS[path] ?? []), { name: 'kirocrew' }, { name: 'kirocrew-dev' }]
}

const realFetch = window.fetch.bind(window)
window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.includes('/api/agents')) {
    const qs = url.split('?')[1] || ''
    const path = decodeURIComponent(new URLSearchParams(qs).get('project_path') || '')
    if (path.includes('err')) {
      // Terminal scan failure (retry:false) → the ErrorNotice + Retry row.
      return Promise.resolve(new Response(JSON.stringify({ error: 'scan failed' }), { status: 500 }))
    }
    if (path.includes('hang')) {
      // Never resolves → the roster stays in flight (loading hint).
      return new Promise<Response>(() => {})
    }
    return Promise.resolve(
      new Response(JSON.stringify({ agents: rosterFor(path), default_agent: '' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
  }
  return realFetch(input as RequestInfo, init)
}) as typeof window.fetch

// --- Props per scene ------------------------------------------------------
const f = (id: string, extra: Partial<ChatFolder> = {}): ChatFolder =>
  ({ id, name: id, order: 0, ...extra }) as ChatFolder

const GLOBAL = [
  { name: 'kirocrew', scope: 'global' },
  { name: 'kirocrew-dev', scope: 'global' },
]
const TAGS: ChatTag[] = [
  { id: 't1', name: 'Payments', color: '#ef4444', order: 0 } as ChatTag,
  { id: 't2', name: 'Urgent', color: '#3b82f6', order: 1 } as ChatTag,
]

interface Scene {
  mode: 'create' | 'edit'
  parentId?: string
  folder?: ChatFolder
  folders: ChatFolder[]
}

function sceneProps(): Scene {
  switch (state) {
    // Dropdown populated for the folder's own project dir.
    case '1':
      return { mode: 'edit', folder: f('f1', { name: 'Payments', project_dir: '/repo/pay', default_agent: 'repo-dev' }), folders: [f('f1', { name: 'Payments', project_dir: '/repo/pay', default_agent: 'repo-dev' })] }
    // Re-scope in flight (loading) and re-scope settled (loaded): seeded with a
    // GLOBAL agent so it stays named through the re-scope. Same seed for both.
    case '2':
    case '3':
      return { mode: 'edit', folder: f('f1', { name: 'Payments', project_dir: '/repo/pay', default_agent: 'kirocrew-dev' }), folders: [f('f1', { name: 'Payments', project_dir: '/repo/pay', default_agent: 'kirocrew-dev' })] }
    // Rescope orphan and scan error are CREATE mode ("Create folder" button).
    case '4':
    case '5':
      return { mode: 'create', parentId: '', folders: [] }
    // Ordinary success: a valid seeded pick for the folder's dir.
    case '6':
    default:
      return { mode: 'edit', folder: f('f1', { name: 'Payments', project_dir: '/repo/pay', default_agent: 'repo-dev' }), folders: [f('f1', { name: 'Payments', project_dir: '/repo/pay', default_agent: 'repo-dev' })] }
  }
}

function Harness() {
  const s = sceneProps()
  const [open, setOpen] = useState(true)
  return (
    <FolderConfigModal
      open={open}
      onClose={() => setOpen(false)}
      mode={s.mode}
      parentId={s.parentId}
      folder={s.folder}
      folders={s.folders}
      installedAgents={GLOBAL}
      globalDefaultAgent="kirocrew"
      availableTags={TAGS}
      onRetryTags={() => {}}
      onSubmit={() => Promise.resolve()}
    />
  )
}

// The reliable way to drive a React controlled input from outside React: set
// the value through the native setter, then dispatch a bubbling input event so
// React's onChange fires (a plain `.value =` or playwright fill does not).
;(window as unknown as { __capSetDir: (v: string) => void }).__capSetDir = (v: string) => {
  const el = document.querySelector('[data-testid="folder-config-project-dir"]') as HTMLInputElement | null
  if (!el) return
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set
  setter?.call(el, v)
  el.dispatchEvent(new Event('input', { bubbles: true }))
}

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={queryClient}>
    <Harness />
  </QueryClientProvider>,
)
