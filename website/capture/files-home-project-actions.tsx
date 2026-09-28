/**
 * Isolated capture entry for the Files tab's per-project terminal action (#1142).
 *
 * WHY ISOLATED: the row is gated on a host that can spawn a terminal, and the
 * header it sits in is gated on a `directLocal` branding read. Both are trivial
 * to establish here and awkward to arrange against a live gateway, where getting
 * the frame also means getting a real chat, a real project and a real PTY into
 * place first.
 *
 * WHAT IS FAITHFUL: the REAL `FilesHomePanel`, inside the real
 * `BrandingProvider` (so `directLocal` — and therefore the Reveal button the new
 * trigger sits beside — comes from the branding read rather than a prop), with
 * the real `FileBrowserRail` mounted over a stubbed project tree. Nothing about
 * the header is faked; only the transport under it is.
 *
 * Query string: ?theme=dark
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// Initialise i18next exactly as main.tsx does — without it every label in the
// frame is blank and the screenshot misrepresents the real UI. Pinned to `en`
// because the driver asserts the English labels.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'
import { ThemeProvider } from '../src/hooks/useTheme'
import { BrandingProvider } from '../src/hooks/useBranding'
import FilesHomePanel from '../src/pages/chat/FilesHomePanel'

initI18n('en')

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'

// `ThemeProvider` is the authority: it reads `mc-theme` and applies the palette
// itself, so setting `data-theme` alone is clobbered on mount (an unset
// preference resolves to `system`, which is LIGHT in headless Chromium). Seed the
// preference it reads, and set the attribute too so the first paint is already
// the right palette.
localStorage.setItem('mc-theme', theme)
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT_DIR = '/Users/me/workspace/KiroCrew'

/** Fetch stub at the API boundary, so nothing hangs on a gateway that does not
 *  exist here. Three reads matter to this header: branding (`directLocal` gates
 *  Reveal), the project tree (mounts the rail), and git status. */
const realFetch = window.fetch.bind(window)
window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (!url.includes('/api/')) return realFetch(input, init)
  const body = url.includes('/api/dashboard/branding')
    ? { bot_name: 'Kiro Crew', avatar: '/logo.png', direct_local: true }
    : url.includes('/api/project/tree')
      ? {
        root: PROJECT_DIR,
        paths: ['README.md', 'src/main.py', 'src/util.py', 'website/src/App.tsx'],
        directories: ['src', 'website', 'website/src'],
        repo: true,
      }
      : url.includes('/api/project/git/status')
        ? { repo: true, branch: 'main', ahead: 0, behind: 0, files: [] }
        : {}
  return Promise.resolve(new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
}

function Harness() {
  return (
    // A right-dock-sized frame, the width the panel occupies in the chat.
    <div
      style={{ width: 460, height: '100vh', marginLeft: 'auto', borderLeft: '1px solid var(--border)', display: 'flex', flexDirection: 'column' }}
      className="bg-bg text-text"
    >
      <FilesHomePanel
        projectDir={PROJECT_DIR}
        onFileOpen={() => {}}
        onOpenTerminal={() => {}}
      />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <ThemeProvider>
      <BrandingProvider>
        <Harness />
      </BrandingProvider>
    </ThemeProvider>
  </QueryClientProvider>,
)
