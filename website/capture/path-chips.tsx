/**
 * Isolated capture entry for markdown path chips and the folder panel.
 *
 * WHY ISOLATED: the chips only reach their interesting states inside a rendered
 * assistant turn, and booting the full SPA to get one needs the app shell, a
 * live websocket and a seeded session — a half-stubbed shell renders its error
 * boundary, which is worse evidence than none.
 *
 * The one thing that MUST be faithful here is the stat probe, because the whole
 * change is "the chip's appearance is decided by the backend, not by a regex".
 * So instead of mocking the component, this stubs `fetch` at the same seam the
 * real hook uses and answers with the same `X-Path-Kind` header the real
 * endpoint sends (see api_file_read in dashboard/handlers/files.py) — the chips
 * then classify themselves exactly as they do in production.
 *
 * Scene + theme come from the query string: ?scene=chips&theme=dark
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
// MarkdownPanel's overflow menu reaches for useNavigate (open-as-artifact), so
// the reveal scene needs a router in scope even though nothing here navigates.
import { MemoryRouter } from 'react-router-dom'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n — without calling it, every label in the frame is blank, which
// silently produces screenshots that misrepresent the real UI.
import { initI18n } from '../src/i18n'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import MarkdownPanel from '../src/components/MarkdownPanel'
import FolderPanel from '../src/pages/chat/FolderPanel'
import { api } from '../src/api/client'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'chips'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** Paths the fake backend reports as directories / files; everything else 404s
 *  as missing, mirroring the real endpoint's three outcomes. */
const DIRS = new Set(['/Users/diwm/.kiro/crew/workspace/KiroCrew', '/Users/diwm/.kiro/crew'])
const FILES = new Set([
  '/Users/diwm/.kiro/crew/workspace/KiroCrew/README.md',
  '/Users/diwm/.kiro/crew/workspace/KiroCrew/src/kiro_crew/acp/_dispatch.py',
  '/Users/diwm/.kiro/crew/workspace/blue-angels-seattle-2026.md',
])

const realFetch = globalThis.fetch.bind(globalThis)
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (url.startsWith('/api/file-read')) {
    const p = decodeURIComponent(new URLSearchParams(url.split('?')[1] || '').get('path') || '')
    if (DIRS.has(p)) {
      return Promise.resolve(new Response(null, { status: 404, headers: { 'X-Path-Kind': 'dir' } }))
    }
    if (FILES.has(p)) {
      return Promise.resolve(new Response(null, { status: 200, headers: { 'X-Path-Kind': 'file' } }))
    }
    return Promise.resolve(new Response(null, { status: 404, headers: { 'X-Path-Kind': 'missing' } }))
  }
  return realFetch(input as RequestInfo, init)
}) as typeof fetch

// The folder scene reads the directory listing through the api client, so stub
// that method rather than the transport — it is the seam FolderPanel owns.
api.browseFiles = (async (p?: string) => ({
  path: p || '/Users/diwm/.kiro/crew/workspace/KiroCrew',
  parent: '/Users/diwm/.kiro/crew/workspace',
  dirs: [
    { name: 'src', path: '/x/src', mtime: 0 },
    { name: 'website', path: '/x/website', mtime: 0 },
    { name: 'docs', path: '/x/docs', mtime: 0 },
  ],
  files: [
    { name: 'README.md', path: '/x/README.md', mtime: 0 },
    { name: 'pyproject.toml', path: '/x/pyproject.toml', mtime: 0 },
    { name: 'Makefile', path: '/x/Makefile', mtime: 0 },
  ],
})) as typeof api.browseFiles

// The reveal scene mounts the real MarkdownPanel, which asks whether this path is
// already tracked as an artifact. Answer "no" rather than let it hit the dev
// server and render an error state over the editor we are trying to photograph.
api.artifacts = (async () => ({ artifacts: [] })) as unknown as typeof api.artifacts

/** The exact message from the bug report: two directory chips and a git ref. */
const TRANSCRIPT = [
  'The worktree is a linked worktree of `/Users/diwm/.kiro/crew/workspace/KiroCrew`.',
  'Its `HEAD` points at `refs/heads/fix/investigation-record-403`',
  '= `4a72aec5f04d3f44ba8042931226db051242d48a` — based on cached `origin/main`.',
  '',
  'Config lives under `/Users/diwm/.kiro/crew` and the readme is at',
  '`/Users/diwm/.kiro/crew/workspace/KiroCrew/README.md`.',
  'A path that is gone: `/Users/diwm/.kiro/crew/deleted-notes.md`.',
].join('\n')

/**
 * Cited source locations — the shape an agent produces when pointing at code.
 *
 * Every one of these was inert before, because the probe was handed the whole
 * token: the backend was asked about a path ending in `:447`, which never
 * exists. The bare `:493` must STAY inert, since no file is named.
 */
const CITED = [
  'Kiro Crew resolves `purpose` in two places:',
  '',
  '- `/Users/diwm/.kiro/crew/workspace/KiroCrew/src/kiro_crew/acp/_dispatch.py:447` — the guard',
  '- same file `:493` — no file is named here, so it stays plain text',
  '- `/Users/diwm/.kiro/crew/workspace/KiroCrew/src/kiro_crew/acp/_dispatch.py:504:12` — line and column',
  '- gone: `/Users/diwm/.kiro/crew/workspace/KiroCrew/src/kiro_crew/acp/missing.py:12`',
  '- a passage, not a statement: `/Users/diwm/.kiro/crew/workspace/blue-angels-seattle-2026.md:10-16`',
].join('\n')

/**
 * Synthetic source for the reveal scene, long enough that line 447 is well off
 * the first screen — otherwise the screenshot could not tell a working scroll
 * from no scroll at all. Line 447 is labelled so the evidence is self-checking.
 */
const MD_SOURCE = Array.from({ length: 60 }, (_, i) => {
  const n = i + 1
  if (n === 10) return '## Saturday — the range starts here (line 10)'
  if (n > 10 && n < 16) return `- schedule item on line ${n}`
  if (n === 16) return 'and the range ends here (line 16).'
  return `filler line ${n}`
}).join('\n')

const PY_SOURCE = Array.from({ length: 700 }, (_, i) => {
  const n = i + 1
  if (n === 447) return '    return _redact(purpose)  # <-- line 447, the cited guard'
  return `    step_${n} = compute(${n})`
}).join('\n')

/** Range reveal, with a control that bumps the nonce so a REPEAT reveal can be
 *  driven — probe-reveal-fade.mjs uses it to prove the highlight relights. */
function RangeScene() {
  const [nonce, setNonce] = useState(1)
  return (
    <div data-capture-root style={{ width: 720, height: 420 }} className="bg-bg">
      <button
        data-testid="reveal-again"
        onClick={() => setNonce(n => n + 1)}
        style={{ position: 'absolute', left: -9999, top: -9999 }}
      >reveal again</button>
      <MarkdownPanel
        embedded
        filePath="/Users/diwm/.kiro/crew/workspace/blue-angels-seattle-2026.md"
        content={MD_SOURCE}
        onContentChange={() => {}}
        onSave={async () => {}}
        onClose={() => {}}
        revealLine={{ line: 10, endLine: 16, nonce }}
      />
    </div>
  )
}

function Scene() {
  if (scene === 'range') return <RangeScene />
  if (scene === 'reveal') {
    // The other half of the feature: the panel a `file.py:447` chip opens must
    // land ON 447 and flash it. Mounted with the REAL panel and a real Monaco so
    // the decoration classes are proven against the theme tokens — a mocked
    // editor would screenshot the mock, not the highlight.
    return (
      <div data-capture-root style={{ width: 720, height: 420 }} className="bg-bg">
        <MarkdownPanel
          embedded
          filePath="/Users/diwm/.kiro/crew/workspace/KiroCrew/src/kiro_crew/acp/_dispatch.py"
          content={PY_SOURCE}
          onContentChange={() => {}}
          onSave={async () => {}}
          onClose={() => {}}
          revealLine={{ line: 447, nonce: 1 }}
        />
      </div>
    )
  }
  if (scene === 'folder') {
    return (
      <div data-capture-root style={{ width: 420, height: 340 }} className="bg-bg">
        <FolderPanel
          path="/Users/diwm/.kiro/crew/workspace/KiroCrew"
          onClose={() => {}}
          onFileOpen={() => {}}
        />
      </div>
    )
  }
  return (
    <div data-capture-root className="bg-bg p-5" style={{ width: 720 }}>
      <MarkdownRenderer content={scene === 'cited' ? CITED : TRANSCRIPT} />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Scene />
    </QueryClientProvider>
  </MemoryRouter>,
)
