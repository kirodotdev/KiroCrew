/**
 * Isolated capture entry for the App SDK's `FileTree`.
 *
 * An app page with the tree on the left and the app's own view of the opened
 * file on the right. `fetch` is stubbed with one project listing and its git
 * status; the tree, the app's scoped client and the stylesheet are the real ones.
 *
 * Scenes (?scene=):
 *   tree      the app declares /api/project/tree only: plain rows.
 *   status    it also declares /api/project/git/status: changed rows coloured.
 *   refused   it declares only /api/project/git/status.
 *   error     the tree read answers 500.
 *   (The shoot script opens a folder, opens a file and types a filter.)
 * Theme: &theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'

import { initI18n } from '../src/i18n'
import { AppApiProvider, FileTree } from '../src/app-sdk/index'
import '../src/index.css'

initI18n('en')

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'tree'
document.documentElement.setAttribute('data-theme', params.get('theme') || 'dark')

const ROOT = '/home/me/widgets'
const TREE = {
  root: ROOT,
  paths: [
    'README.md', 'package.json', 'tsconfig.json',
    'docs/guide.md', 'docs/release-notes.md',
    'src/app.tsx', 'src/index.ts', 'src/components/Button.tsx', 'src/components/List.tsx',
    'tests/app.test.ts',
  ],
  directories: ['docs/', 'src/', 'src/components/', 'tests/'],
  repo: true,
}
const STATUS = {
  repo: true, repoRoot: ROOT, branch: 'main',
  files: [
    { path: 'src/app.tsx', status: 'M', staged: false, additions: 12, deletions: 3 },
    { path: 'src/components/List.tsx', status: 'A', staged: true, additions: 40, deletions: 0 },
    { path: 'docs/release-notes.md', status: '?', staged: false },
  ],
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const realFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const url = new URL(typeof input === 'string' ? input : input instanceof URL ? input.href : input.url, location.href)
  if (url.pathname === '/api/project/tree') {
    return scene === 'error' ? json({ error: 'internal error' }, 500) : json(TREE)
  }
  if (url.pathname === '/api/project/git/status') {
    return scene === 'status-error' ? json({ error: 'git status failed', code: 'git_status_failed' }, 503) : json(STATUS)
  }
  return realFetch(input, init)
}

const declared = scene === 'refused' ? ['/api/project/git/status']
  : scene === 'status' || scene === 'status-error' ? ['/api/project/tree', '/api/project/git/status']
    : ['/api/project/tree']

function Page() {
  const [opened, setOpened] = useState<string | null>(null)
  const [filter, setFilter] = useState('')
  return (
    <div data-testid="frame" style={{ height: 520, maxWidth: 760 }} className="mx-auto flex overflow-hidden rounded-lg border border-border bg-bg-elevated">
      <div style={{ width: 280, display: 'flex', flexDirection: 'column' }} className="shrink-0 border-r border-border">
        <input
          aria-label="Filter files"
          placeholder="Filter files"
          value={filter}
          onChange={e => setFilter(e.target.value)}
          className="m-2 h-[28px] rounded-md border border-border bg-bg px-2 text-[12px] text-text"
        />
        <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>
          <FileTree projectDir={ROOT} onOpenFile={setOpened} selectedPath={opened} searchQuery={filter} />
        </div>
      </div>
      <div className="flex-1 p-4 text-[13px] text-muted" data-testid="opened">
        {opened ? <>The app opened <code className="text-text">{opened}</code></> : 'Pick a file in the tree.'}
      </div>
    </div>
  )
}

createRoot(document.getElementById('root')!).render(
  <div className="min-h-screen bg-bg p-4 text-text">
    <AppApiProvider appName="file-tree-demo" allowedApiPaths={declared} navigateFn={() => {}}>
      <Page />
    </AppApiProvider>
  </div>,
)
