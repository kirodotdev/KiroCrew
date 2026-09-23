/**
 * Isolated capture entry for the HOSTLESS file-tree row menu (issue #9665).
 *
 * WHY ISOLATED: the gateway-free SPA harness
 * (`website/capture/shoot-tree-download-menu.mjs`) drives the chat route, which
 * always wires `onAddToContext`, so it can only show the two-item file menu. The
 * hostless state -- a file row whose only menu item is `Download`, no
 * `Add to chat` -- ships on the Members DM Files tab, where the panel mounts the
 * tree with NO host. That route cannot be furnished from the stubbed `/api/**`
 * harness, so this entry renders `PierreWorkspaceTree` directly with
 * `onAddToContext` omitted and stubs only the two project endpoints the tree
 * reads. The render is REAL: the same component, the same `@pierre/trees`
 * runtime, the same portal menu -- only the host prop is absent, exactly as on
 * the members route.
 *
 * Theme comes from the query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { initI18n } from '../src/i18n/all'
import { PierreWorkspaceTree } from '../src/pierre/tree'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const ROOT = '/workspace/project'
const PATHS = ['README.md', 'src/index.ts', 'src/app/main.tsx', 'docs/guide.md']

// Stub ONLY the two endpoints the tree reads; everything else falls through.
// The workspace is git-free so no status lanes distract from the menu.
const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/project/tree')) {
    return Promise.resolve(new Response(JSON.stringify({ root: ROOT, paths: PATHS, repo: false }), {
      status: 200, headers: { 'content-type': 'application/json' },
    }))
  }
  if (url.startsWith('/api/project/git/status')) {
    return Promise.resolve(new Response(JSON.stringify({ repo: false, files: [] }), {
      status: 200, headers: { 'content-type': 'application/json' },
    }))
  }
  return realFetch(input, init)
}) as typeof globalThis.fetch

async function main() {
  initI18n('en')
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const root = createRoot(document.getElementById('root')!)
  root.render(
    <QueryClientProvider client={qc}>
      <div className="bg-bg text-text" style={{ width: 360, height: 520, display: 'flex', flexDirection: 'column' }}>
        {/* onAddToContext omitted on purpose: this is the Members-DM hostless
            mount, where a file row's menu carries Download alone. */}
        <PierreWorkspaceTree projectDir={ROOT} />
      </div>
    </QueryClientProvider>,
  )
}

void main()
