/**
 * Isolated capture entry for the folder-panel read-failure notices, the header
 * Refresh label, and the file rail kept mounted on a recoverable tree failure.
 *
 * WHY ISOLATED: every state here is a FAILED read, which the full SPA reaches
 * only through a wedged gateway — booting the shell against a half-stubbed
 * backend photographs its own error boundary instead.
 *
 * Faithfulness is in the ERRORS: each rejection is the exact shape production
 * raises — a named `TimeoutError`, a coded 403, and the code-less 400 the listing
 * endpoint returns for a path that is no longer a directory — so the components
 * classify themselves rather than being told what to render.
 *
 * Scene + theme: ?scene=listing-timeout&theme=dark
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

// Without the call every label in the frame is blank, which silently produces
// screenshots that misrepresent the real UI.
import { initI18n } from '../src/i18n'
import FolderPanel from '../src/pages/chat/FolderPanel'
import FileBrowserRail from '../src/pages/chat/FileBrowserRail'
import FilesHomePanel from '../src/pages/chat/FilesHomePanel'
import { api } from '../src/api/client'
import { ApiError } from '../src/api/apiError'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = params.get('scene') || 'listing-timeout'
const theme = params.get('theme') || 'dark'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

const PROJECT = '/Demo Workspace/Product Guide'
/** A SUBDIRECTORY, so the panel is in listing mode. At the project root a ready
 *  tree takes over the body and no listing notice can render at all, so a listing
 *  scene pointed there would photograph the tree and prove nothing. */
const SUBDIR = `${PROJECT}/src`

/** Scenes whose evidence lives at the project ROOT: the tree-failure notice, and
 *  the one-outage-one-notice rule that only arises when both reads fail there. */
const ROOT_SCENES = new Set(['tree-recoverable', 'one-notice-not-two'])

/** The deadline rejection `withDeadline` raises, by name — `searchErrorCause`
 *  reads `isDeadlineError`, so a plain Error would classify as `failed` and the
 *  frame would show the wrong notice while still looking like a failure. */
const timeout = () => Object.assign(new Error('deadline exceeded'), { name: 'TimeoutError' })

/** `/api/file-search`'s refusal: a coded 403, which is what makes it `denied`
 *  rather than the generic failure. */
const denied = () => new ApiError(403, 'denied', JSON.stringify({ code: 'access_denied' }))

/** `/api/browse-files`' answer for a path that is no longer a directory: a coded 400. */
const notADirectory = () => new ApiError(400, 'Not a directory',
  JSON.stringify({ error: 'Not a directory', code: 'not_a_directory' }))

const listing = (p?: string) => ({
  path: p || PROJECT,
  parent: '/Demo Workspace',
  dirs: [
    { name: 'src', path: `${PROJECT}/src`, mtime: 0 },
    { name: 'docs', path: `${PROJECT}/docs`, mtime: 0 },
  ],
  files: [
    { name: 'README.md', path: `${PROJECT}/README.md`, mtime: 0 },
    { name: 'release-notes.md', path: `${PROJECT}/release-notes.md`, mtime: 0 },
  ],
})

const hits = {
  root: PROJECT,
  results: [
    { path: `${PROJECT}/src/overview.md`, name: 'overview.md', size: 812, mtime: 0, kind: 'file' as const },
    { path: `${PROJECT}/src/oauth-notes.md`, name: 'oauth-notes.md', size: 441, mtime: 0, kind: 'file' as const },
  ],
}

const tree = {
  root: PROJECT,
  paths: ['README.md', 'docs/getting-started.md', 'src/overview.md'],
  repo: false,
}

/* Default every seam to a SUCCEEDING read, so each scene below states only the
 * one failure it is evidence for. A scene that inherited a second failure would
 * photograph two problems and prove neither. */
api.browseFiles = (async (p?: string) => listing(p)) as typeof api.browseFiles
api.projectTree = (async () => tree) as typeof api.projectTree
api.projectGitStatus = (async () => ({ repo: false, files: [] })) as typeof api.projectGitStatus
api.fileSearch = (async () => hits) as unknown as typeof api.fileSearch
api.revealPath = (async () => undefined) as unknown as typeof api.revealPath

/** A search that has ALREADY answered once and fails on the refetch, so the
 *  panel holds its previous rows under the notice instead of blanking. */
let searchCalls = 0
function searchFailsOnRefetch() {
  api.fileSearch = (async () => {
    searchCalls += 1
    if (searchCalls > 1) throw timeout()
    return hits
  }) as unknown as typeof api.fileSearch
}

switch (scene) {
  case 'search-timeout':
    api.fileSearch = (async () => { throw timeout() }) as unknown as typeof api.fileSearch
    break
  case 'search-denied':
    api.fileSearch = (async () => { throw denied() }) as unknown as typeof api.fileSearch
    break
  case 'listing-timeout':
    api.browseFiles = (async () => { throw timeout() }) as typeof api.browseFiles
    break
  case 'listing-missing':
    api.browseFiles = (async () => { throw notADirectory() }) as typeof api.browseFiles
    break
  case 'listing-denied':
    api.browseFiles = (async () => { throw denied() }) as typeof api.browseFiles
    break
  case 'stale-rows':
    searchFailsOnRefetch()
    break
  case 'refresh-morph': {
    // The first listing answers, so the control starts icon-only; the refetch holds long enough
    // for the reserved width to be visible, then fails so the label resolves.
    let n = 0
    api.browseFiles = (async () => {
      n += 1
      if (n === 1) return listing()
      await new Promise(r => setTimeout(r, 1400))
      throw timeout()
    }) as typeof api.browseFiles
    break
  }
  case 'tree-recoverable':
  case 'rail-recoverable':
  case 'files-home-recoverable':
    // The LISTING still answers, so the rail must stay mounted naming the tree.
    api.projectTree = (async () => { throw timeout() }) as typeof api.projectTree
    break
  case 'files-home-refusal':
    // A refusal, so the rail stays hidden and the surface must name WHICH failure it was.
    api.projectTree = (async () => { throw denied() }) as typeof api.projectTree
    break
  case 'one-notice-not-two':
    // One wedged gateway, both reads. The tree notice is withheld so a single
    // outage does not read as two problems.
    api.projectTree = (async () => { throw timeout() }) as typeof api.projectTree
    api.browseFiles = (async () => { throw timeout() }) as typeof api.browseFiles
    break
  default:
    break
}

function Panel({ width = 420, height = 360 }: { width?: number; height?: number }) {
  const atRoot = ROOT_SCENES.has(scene)
  return (
    <div data-capture-root style={{ width, height }} className="bg-bg">
      <FolderPanel
        path={atRoot ? PROJECT : SUBDIR}
        projectDir={PROJECT}
        onClose={() => {}}
        onFileOpen={() => {}}
      />
    </div>
  )
}

function Scene() {
  if (scene === 'rail-recoverable') {
    return (
      <div data-capture-root style={{ width: 360, height: 360 }} className="flex bg-bg">
        <FileBrowserRail projectDir={PROJECT} onFileOpen={() => {}} />
      </div>
    )
  }
  if (scene === 'files-home-recoverable' || scene === 'files-home-refusal') {
    return (
      <div data-capture-root style={{ width: 760, height: 420 }} className="bg-bg">
        <FilesHomePanel projectDir={PROJECT} onFileOpen={() => {}} />
      </div>
    )
  }
  return <Panel />
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
