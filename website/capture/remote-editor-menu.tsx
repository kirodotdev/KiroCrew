/**
 * Isolated capture entry for the FilePathMenu "Open in <editor>" row
 * (PR #11345 / issue #11338).
 *
 * WHY ISOLATED: the row renders only inside a file chip's context menu within a
 * rendered assistant turn, and it is gated on BrandingProvider state:
 * `remote_editor.{editor,host}` configured. `direct_local` does not gate it, so
 * the `local` axis renders the ssh -L tunnel case where the gateway reads true.
 * Booting the full SPA to reach that state needs the app shell, a websocket and
 * a seeded session; a half-stubbed shell renders its error boundary, which is
 * worse evidence than none (same reasoning as path-chips.tsx).
 *
 * Two things MUST be faithful here:
 *  - the stat probe (the batched `POST /api/file-kinds` reply), so the chip
 *    classifies itself exactly as in production, and
 *  - the branding read (`/api/dashboard/branding` through the REAL
 *    BrandingProvider), because the row's whole gate lives on that hook —
 *    stubbing the transport rather than the hook proves the gate.
 *
 * Scene axes from the query string:
 *   ?theme=dark|light&editor=vscode|kiro&local=0|1&scene=chips|markdown-panel|fileviewer
 *
 * `scene` picks the surface carrying the row (default `chips`):
 *  - chips           the transcript's file/dir chips (FilePathMenu context menu)
 *  - markdown-panel  the REAL MarkdownPanel (embedded, as the side panel mounts
 *                    it) on the config file; its "⋯" OverflowMenu holds the row
 *  - fileviewer      the REAL Files-app FileViewer on the config file; its
 *                    "More options" DropdownMenu holds the row
 * Both panel scenes go through the same BrandingProvider + fetch seam, so the
 * row's gate is the production one in every scene.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does — without it every label in the
// frame is blank (see path-chips.tsx).
import { initI18n } from '../src/i18n'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import MarkdownPanel from '../src/components/MarkdownPanel'
import FileViewer from '../src/apps/file-explorer/FileViewer'
import { BrandingProvider } from '../src/hooks/useBranding'
import '../src/index.css'
// FileViewer's bar classes (mc-fe-viewer-bar, mc-fe-iconbtn, ...) live in the
// Files app's FE_CSS sheet, which FileExplorerPage injects as a <style> inside
// its .mc-fe-root; the fileviewer scene does exactly the same.
import { FE_CSS } from '../src/apps/file-explorer/styles'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const editor = params.get('editor') || 'vscode'
const directLocal = params.get('local') === '1'
const scene = params.get('scene') || 'chips'

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
  // Chips classify through the batched probe (usePathKind), one POST per commit.
  if (url.startsWith('/api/file-kinds')) {
    const { paths = [] } = JSON.parse(String(init?.body || '{}')) as { paths?: string[] }
    const kinds: Record<string, string> = {}
    for (const raw of paths) {
      const p = raw.length > 1 && /[\\/]$/.test(raw) ? raw.slice(0, -1) : raw
      kinds[raw] = DIRS.has(p) ? 'dir' : FILES.has(p) ? 'file' : 'missing'
    }
    return Promise.resolve(new Response(JSON.stringify({ kinds }), {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
  }
  // The one read the row's gate lives on: an editor and host configured,
  // exactly what api_branding sends. direct_local follows the `local` axis.
  if (url.startsWith('/api/dashboard/branding')) {
    return Promise.resolve(new Response(JSON.stringify({
      bot_name: 'Kiro Crew', avatar: '/logo.png', direct_local: directLocal,
      remote_editor: { editor, host: HOST },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  }
  // ── Panel scenes only (markdown-panel / fileviewer) ──
  // The reads MarkdownPanel's OverflowMenu makes on mount, answered in the
  // gateway-free shape the real handlers send, so the menu renders its normal
  // rows instead of an error notice over the panel.
  const ok = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
  // knowledge.get_config with no pipeline (gateway-free): enabled false, so
  // the menu offers no "Add to knowledge" row — as on a host without one.
  if (url.startsWith('/api/knowledge/config')) {
    return ok({ enabled: false, supported_formats: [], accepts_no_extension: true, folder_picker: false })
  }
  if (url.startsWith('/api/knowledge/sources')) return ok([])
  // Not yet an artifact: the "Add to artifacts" row, not "In artifacts".
  if (url.startsWith('/api/artifacts')) return ok({ artifacts: [] })
  // No installed apps: no "Open in Files" row and no app-contributed rows.
  if (url.startsWith('/api/apps')) return ok([])
  // A config file outside any repo: no diff to offer.
  if (url.startsWith('/api/file-diff')) return ok({ diff: '', original: '', status: 'not_git' })
  if (url.startsWith('/api/')) {
    // Surfaced so a new mount-time read is discoverable rather than silently
    // answered by the dev server's 404.
    console.warn(`CAPTURE-STUB: no fixture for ${url}`)
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

/** The file both panel scenes show: the gateway config, as the transcript names. */
const CONFIG_BODY = JSON.stringify({
  dashboard: { remote_editor: { editor, host: HOST } },
  agent: { model: 'auto' },
}, null, 2)

function Scene() {
  if (scene === 'markdown-panel') {
    // Embedded, exactly as the chat side panel mounts it for a file tab.
    return (
      <div data-capture-root className="bg-bg" style={{ width: 760, height: '100vh' }}>
        <MarkdownPanel
          embedded
          filePath={CONFIG}
          content={CONFIG_BODY}
          onContentChange={() => {}}
          onSave={async () => {}}
          onClose={() => {}}
        />
      </div>
    )
  }
  if (scene === 'fileviewer') {
    // The Files app's right pane: FE_CSS inside .mc-fe-root, then .mc-fe-right
    // around the viewer, as FileExplorerPage renders it.
    return (
      <div data-capture-root className="mc-fe-root" style={{ width: 760, height: '100vh' }}>
        <style>{FE_CSS}</style>
        <div className="mc-fe-right" style={{ height: '100%' }}>
          <FileViewer
            filePath={CONFIG}
            fileMeta={{ size: CONFIG_BODY.length, mtime: 1760300000, mime: 'application/json', encoding: 'utf-8' }}
            content={CONFIG_BODY}
            loading={false}
            error={null}
            onReload={() => {}}
            onDownload={() => {}}
          />
        </div>
      </div>
    )
  }
  return (
    <div data-capture-root className="bg-bg p-5" style={{ width: 760, height: 420 }}>
      <MarkdownRenderer content={TRANSCRIPT} onFileOpen={() => {}} />
    </div>
  )
}

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <BrandingProvider>
        <Scene />
      </BrandingProvider>
    </QueryClientProvider>
  </MemoryRouter>,
)
