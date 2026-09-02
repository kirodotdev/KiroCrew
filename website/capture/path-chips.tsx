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
const WORKSPACE = '/Demo Workspace'
const PROJECT = `${WORKSPACE}/Product Guide`
const RELEASE_NOTES = `${PROJECT}/release-notes.md`
const OVERVIEW = `${PROJECT}/src/overview.md`

const DIRS = new Set([PROJECT, WORKSPACE])

/** Unicode paths — issue #6483: none of these classified before PATH_SHAPE_RE
 *  gained `\p{L}\p{M}\p{N}` + the `u` flag, so the probe was never issued. The
 *  NFD constant is written with escapes (e + U+0301) exactly as macOS returns
 *  decomposed filenames. */
const CJK_DOC = `${PROJECT}/产品文档/发布说明.md`
const NFD_NOTES = `${PROJECT}/cafe\u0301-menu\u0308/notes.md`
const DEVANAGARI_REPORT = '~/दस्तावेज़/रिपोर्ट.md'
const FILES = new Set([
  `${PROJECT}/README.md`,
  OVERVIEW,
  RELEASE_NOTES,
  CJK_DOC,
  NFD_NOTES,
  DEVANAGARI_REPORT,
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
  path: p || PROJECT,
  parent: WORKSPACE,
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

// A project-root folder tab uses the same real Pierre tree as the dashboard.
// Only the transport is stubbed; expand/collapse and row rendering stay real.
api.projectTree = (async () => ({
  root: PROJECT,
  paths: [
    'README.md',
    'docs/getting-started.md',
    'src/overview.md',
    'src/components/Header.tsx',
    'website/package.json',
  ],
  repo: false,
})) as typeof api.projectTree
api.projectGitStatus = (async () => ({ repo: false, files: [] })) as typeof api.projectGitStatus

// The reveal scene mounts the real MarkdownPanel, which asks whether this path is
// already tracked as an artifact. Answer "no" rather than let it hit the dev
// server and render an error state over the editor we are trying to photograph.
api.artifacts = (async () => ({ artifacts: [] })) as unknown as typeof api.artifacts

/** A neutral sample message: two directory chips and a git ref. */
const TRANSCRIPT = [
  'The sample project is a linked worktree at `/Demo Workspace/Product Guide`.',
  'Its `HEAD` points at `refs/heads/fix/investigation-record-403`',
  '= `4a72aec5f04d3f44ba8042931226db051242d48a` — based on cached `origin/main`.',
  '',
  'Shared resources live under `/Demo Workspace` and the readme is at',
  '`/Demo Workspace/Product Guide/README.md`.',
  'A path that is gone: `/Demo Workspace/deleted-notes.md`.',
].join('\n')

/**
 * Cited source locations — the shape an agent produces when pointing at code.
 *
 * Every one of these was inert before, because the probe was handed the whole
 * token: the backend was asked about a path ending in `:447`, which never
 * exists. The bare `:493` must STAY inert, since no file is named.
 */
const CITED = [
  'The release guide resolves `purpose` in two places:',
  '',
  '- `/Demo Workspace/Product Guide/src/overview.md:447` — the guard',
  '- same file `:493` — no file is named here, so it stays plain text',
  '- `/Demo Workspace/Product Guide/src/overview.md:504:12` — line and column',
  '- gone: `/Demo Workspace/Product Guide/src/missing.md:12`',
  '- a passage, not a statement: `/Demo Workspace/Product Guide/release-notes.md:10-16`',
].join('\n')

const LINKED_RELEASE_NOTES = [
  '## Release review',
  '',
  'Open [the release notes](/Demo%20Workspace/Product%20Guide/release-notes.md:12) to inspect the decision.',
  '',
  'The [release checklist](/artifacts/release-checklist) remains an artifact link.',
].join('\n')

const RELEASE_NOTES_SOURCE = Array.from({ length: 30 }, (_, i) => {
  const n = i + 1
  if (n === 12) return 'Decision: approve the release candidate.'
  return `Release note ${n}`
}).join('\n')

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
        filePath={RELEASE_NOTES}
        content={MD_SOURCE}
        onContentChange={() => {}}
        onSave={async () => {}}
        onClose={() => {}}
        revealLine={{ line: 10, endLine: 16, nonce }}
      />
    </div>
  )
}

function MarkdownLinkScene() {
  const [opened, setOpened] = useState<{ path: string; line?: number } | null>(null)
  return (
    <div data-capture-root style={{ width: 900, height: 460 }} className="flex gap-4 bg-bg p-5">
      <section className="min-w-0 flex-1" aria-label="Rendered markdown">
        <MarkdownRenderer
          content={LINKED_RELEASE_NOTES}
          onArtifactOpen={() => {}}
          onFileOpen={(path, opts) => setOpened({ path, line: opts?.line })}
        />
      </section>
      {opened && (
        <section className="w-[440px] overflow-hidden rounded border border-border" aria-label="Opened file panel">
          <MarkdownPanel
            embedded
            filePath={opened.path}
            content={RELEASE_NOTES_SOURCE}
            onContentChange={() => {}}
            onSave={async () => {}}
            onClose={() => {}}
            revealLine={opened.line ? { line: opened.line, nonce: 1 } : undefined}
          />
        </section>
      )}
    </div>
  )
}

function FolderFlowScene() {
  const [active, setActive] = useState<'tree' | 'file'>('tree')
  const [opened, setOpened] = useState<string | null>(null)
  const openFile = (path: string) => {
    setOpened(path)
    setActive('file')
  }
  return (
    <div data-capture-root style={{ width: 720, height: 420 }} className="flex flex-col bg-bg">
      <div className="flex items-center gap-1 h-[38px] px-2 shrink-0 border-b border-border" role="tablist">
        <button
          role="tab"
          aria-selected={active === 'tree'}
          onClick={() => setActive('tree')}
          className={`h-7 px-2 rounded-md text-[12px] border-none cursor-pointer ${active === 'tree' ? 'bg-border text-accent' : 'bg-transparent text-muted'}`}
        >Project tree</button>
        {opened && (
          <button
            role="tab"
            aria-selected={active === 'file'}
            onClick={() => setActive('file')}
            className={`h-7 px-2 rounded-md text-[12px] border-none cursor-pointer ${active === 'file' ? 'bg-border text-accent' : 'bg-transparent text-muted'}`}
          >{opened.split('/').pop()}</button>
        )}
      </div>
      <div className="relative flex-1 min-h-0">
        <div className="absolute inset-0" style={{ display: active === 'tree' ? 'block' : 'none' }}>
          <FolderPanel
            path={PROJECT}
            projectDir={PROJECT}
            onClose={() => {}}
            onFileOpen={openFile}
          />
        </div>
        {opened && (
          <div className="absolute inset-0" style={{ display: active === 'file' ? 'block' : 'none' }}>
            <MarkdownPanel
              embedded
              filePath={opened}
              content={'# Project overview\n\nThis file opened in a separate tab. The Project tree stays intact.'}
              onContentChange={() => {}}
              onSave={async () => {}}
              onClose={() => setActive('tree')}
            />
          </div>
        )}
      </div>
    </div>
  )
}

/** Unicode transcript for issue #6483: rooted CJK, NFD-decomposed accented,
 *  and home-relative Devanagari paths must classify; slash-separated prose
 *  must stay plain. */
const UNICODE = [
  'The launch doc lives at `' + CJK_DOC + '`,',
  'the macOS notes folder holds `' + NFD_NOTES + '` (NFD-decomposed),',
  'and the report is at `' + DEVANAGARI_REPORT + '`.',
  '',
  'Prose stays plain: `要么这样/要么那样` and `и/или` are not paths.',
].join('\n')

function Scene() {
  if (scene === 'folder-flow') return <FolderFlowScene />
  if (scene === 'markdown-link') return <MarkdownLinkScene />
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
          filePath={OVERVIEW}
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
          path={PROJECT}
          projectDir={PROJECT}
          onClose={() => {}}
          onFileOpen={() => {}}
        />
      </div>
    )
  }
  return (
    <div data-capture-root className="bg-bg p-5" style={{ width: 720 }}>
      <MarkdownRenderer
        content={scene === 'cited' ? CITED : scene === 'unicode' ? UNICODE : TRANSCRIPT}
        onFileOpen={() => {}}
        onFolderOpen={() => {}}
      />
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// The reveal wording follows the GATEWAY's platform, so it is a scene axis:
// `?platform=darwin|win32|linux`. Seeded into the query the prerequisite gate
// owns in the real app, which is the cache `useGatewayPlatform` reads — so the
// components resolve their label exactly as they do in production. Absent, the
// cache stays empty and the generic wording renders, which is also what an
// unreadable platform gets.
const platform = params.get('platform')
if (platform) qc.setQueryData(['kiro-prerequisite'], { platform })

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <QueryClientProvider client={qc}>
      <Scene />
    </QueryClientProvider>
  </MemoryRouter>,
)
