/**
 * Isolated capture entry for the folder tab's listing row menu (#43258): a
 * folder tab opened from a chat path chip on a directory that is NOT the chat's
 * project root (`/tmp/pr42850-evidence`), where the panel renders its one-level
 * listing. Each row now opens the shared `FileRowMenu` the Files tab's tree uses,
 * so Add to chat and Download reach a `/tmp/...` folder.
 *
 * The component is the real one, imported unmodified; only `api.browseFiles` is
 * a fixture, stubbed on the api client the same way the suite stubs it. What a
 * screenshot falsifies here and a test cannot: that the hover "..." button sits
 * where the tree's does, that the portaled menu lands beside the row instead of
 * clipping in the panel, and that both read in dark and light themes.
 *
 * WHY ISOLATED: the side panel needs the app shell, a live websocket and a seeded
 * chat to mount a folder tab; a half-stubbed shell renders its error boundary.
 *
 * `theme` comes from the query string: ?theme=light. `?open=<file>` opens the
 * row menu for that file from its hover button once the listing has rendered,
 * so the frame can be taken with the menu up; `?hover=<file>` only reveals that
 * row's button (the hover state a screenshot cannot otherwise hold).
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import FolderPanel from '../src/pages/chat/FolderPanel'
import { api } from '../src/api/client'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)

const CWD = '/tmp/pr42850-evidence'
const LISTING = {
  path: CWD,
  parent: '/tmp',
  dirs: [{ name: 'frames', path: `${CWD}/frames`, mtime: 1 }],
  files: [
    { name: '01-before-dark.png', path: `${CWD}/01-before-dark.png`, mtime: 2 },
    { name: '02-after-dark.png', path: `${CWD}/02-after-dark.png`, mtime: 3 },
    { name: '03-menu-light.png', path: `${CWD}/03-menu-light.png`, mtime: 4 },
    { name: 'notes.md', path: `${CWD}/notes.md`, mtime: 5 },
  ],
}
api.browseFiles = async () => LISTING as Awaited<ReturnType<typeof api.browseFiles>>
// The listing does not reach the tree (the tab is not at the project root), and
// an isolated page has no apps: an empty list keeps the menu to its built-ins.
api.listApps = async () => [] as Awaited<ReturnType<typeof api.listApps>>

/** The side panel's width and chrome: a 360px column with the panel border,
 *  as the dashboard renders a folder tab beside the chat. */
function Scene() {
  return (
    <div style={{ height: '100vh', display: 'flex', justifyContent: 'flex-end' }} className="bg-bg text-text">
      <div style={{ width: 360, height: '100%', display: 'flex', flexDirection: 'column' }} className="border-l border-border bg-bg">
        <FolderPanel
          path={CWD}
          projectDir="/home/dev/project"
          onClose={() => {}}
          onFileOpen={() => {}}
          onAddToContext={() => {}}
        />
      </div>
    </div>
  )
}

initI18n('en')
const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
createRoot(document.getElementById('root')!).render(
  <QueryClientProvider client={qc}>
    <Scene />
  </QueryClientProvider>,
)
