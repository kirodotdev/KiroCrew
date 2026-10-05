/**
 * Isolated capture entry for where the project picker's Browse pane opens (#17129).
 *
 * The component is the real one, imported unmodified; only the two reads behind it are
 * fixtures, stubbed on the api client the same way the suite stubs them. `?start=` is the
 * caller's current selection, passed as `startPath`; without it the pane opens at the
 * home listing, which is how every mount opened it before. `?start=` naming the deleted
 * fixture shows the fallback: the home listing under a notice naming the selection. Recents are empty so the
 * picker lands on Browse without a click.
 *
 * WHY ISOLATED: the picker is a portalled popover anchored to a measured rect inside
 * ChatPage, which needs the app shell, a live websocket and a seeded session to reach;
 * a half-stubbed shell renders its error boundary instead. `anchorRef` supplies the
 * measurement the shell would.
 */
import { createRoot } from 'react-dom/client'
import { useRef } from 'react'
import ProjectPicker from '../src/components/ProjectPicker'
import { api } from '../src/api/client'
import { ApiError } from '../src/api/apiError'
// The tab labels ARE catalog strings, so an uninitialised i18n would render them empty.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)
const start = params.get('start') || ''

const LISTINGS: Record<string, { path: string; parent: string; dirs: { name: string; path: string }[] }> = {
  '/home/dev': { path: '/home/dev', parent: '/home', dirs: [
    { name: 'notes', path: '/home/dev/notes' },
    { name: 'projects', path: '/home/dev/projects' },
  ] },
  '/home/dev/projects/api-gateway': { path: '/home/dev/projects/api-gateway', parent: '/home/dev/projects', dirs: [
    { name: 'docs', path: '/home/dev/projects/api-gateway/docs' },
    { name: 'src', path: '/home/dev/projects/api-gateway/src' },
    { name: 'test', path: '/home/dev/projects/api-gateway/test' },
  ] },
}
api.recentProjects = async () => ({ dirs: [] })
// `/home/dev/projects/old-service` is a selection that was deleted since it was chosen: the
// gateway answers `not_a_directory`, as /api/browse-dirs does for a path that no longer lists.
const GONE = '/home/dev/projects/old-service'
api.browseDirs = async (path?: string) => {
  if (path === GONE) {
    throw new ApiError(400, 'Not a directory', JSON.stringify({ error: 'Not a directory', code: 'not_a_directory' }))
  }
  return LISTINGS[path || '/home/dev'] ?? LISTINGS['/home/dev']
}

/** The anchor the chat composer's project chip would be: bottom-right, so the popover
 *  flips upward exactly as it does in the dashboard. */
function Scene() {
  const anchor = useRef<HTMLButtonElement>(null)
  return (
    <div style={{ height: '100vh', position: 'relative' }}>
      <button
        ref={anchor}
        style={{ position: 'absolute', bottom: 16, right: 16 }}
        className="px-2 py-1 text-[12px] text-muted border border-border rounded"
      >
        {start ? start.split('/').pop() : 'No project'}
      </button>
      <ProjectPicker open onOpenChange={() => {}} anchorRef={anchor} onSelect={() => {}} startPath={start} />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
