/**
 * Isolated capture entry for the FilePathMenu "Open in <editor>" row
 * (PR #11345 / issue #11338).
 *
 * WHY ISOLATED: the row renders only inside a file chip's context menu within a
 * rendered assistant turn, and it is gated on BrandingProvider state — a REMOTE
 * session (`direct_local: false`) with `remote_editor.{editor,host}` configured.
 * Booting the full SPA to reach that state needs the app shell, a websocket and
 * a seeded session; a half-stubbed shell renders its error boundary, which is
 * worse evidence than none (same reasoning as path-chips.tsx).
 *
 * Two things MUST be faithful here:
 *  - the stat probe (`/api/file-read` with the real `X-Path-Kind` header), so
 *    the chip classifies itself exactly as in production, and
 *  - the branding read (`/api/dashboard/branding` through the REAL
 *    BrandingProvider), because the row's whole gate lives on that hook —
 *    stubbing the transport rather than the hook proves the gate.
 *
 * Scene axes from the query string: ?theme=dark|light&editor=vscode|kiro
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does — without it every label in the
// frame is blank (see path-chips.tsx).
import { initI18n } from '../src/i18n'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { BrandingProvider } from '../src/hooks/useBranding'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const editor = params.get('editor') || 'vscode'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** The remote gateway host the configured link targets. */
const HOST = 'dev-host.example.com'
const PROJECT = '/home/user/.kiro/crew'
const CONFIG = `${PROJECT}/config.json`

const DIRS = new Set([PROJECT])
const FILES = new Set([CONFIG])

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/file-read')) {
    let p = decodeURIComponent(new URLSearchParams(url.split('?')[1] || '').get('path') || '')
    if (p.length > 1 && (p.endsWith('/') || p.endsWith('\\'))) p = p.slice(0, -1)
    if (DIRS.has(p)) {
      return Promise.resolve(new Response(null, { status: 404, headers: { 'X-Path-Kind': 'dir' } }))
    }
    if (FILES.has(p)) {
      return Promise.resolve(new Response(null, { status: 200, headers: { 'X-Path-Kind': 'file' } }))
    }
    return Promise.resolve(new Response(null, { status: 404, headers: { 'X-Path-Kind': 'missing' } }))
  }
  // The one read the row's gate lives on: a REMOTE session (direct_local false)
  // with an editor and host configured, exactly what api_branding sends.
  if (url.startsWith('/api/dashboard/branding')) {
    return Promise.resolve(new Response(JSON.stringify({
      bot_name: 'Kiro Crew', avatar: '/logo.png', direct_local: false,
      remote_editor: { editor, host: HOST },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

/** A remote-session transcript naming the file and the directory the feature
 *  was built for: editing gateway config over the user's own SSH session. */
const TRANSCRIPT = [
  'The gateway config lives at `' + CONFIG + '`.',
  'The whole tree is at `' + PROJECT + '` if you want to open it as a workspace.',
].join('\n\n')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <BrandingProvider>
        <div data-capture-root className="bg-bg p-5" style={{ width: 760, height: 420 }}>
          <MarkdownRenderer content={TRANSCRIPT} onFileOpen={() => {}} />
        </div>
      </BrandingProvider>
    </QueryClientProvider>
  </MemoryRouter>,
)
