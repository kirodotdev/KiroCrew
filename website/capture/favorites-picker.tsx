/**
 * Isolated capture entry for the project picker's Favourites tab.
 *
 * The component is the real one, imported unmodified; only the three reads behind it
 * are fixtures, stubbed on the api client the same way the suite stubs them. What a
 * screenshot falsifies here and a test cannot: that the star reads as on or off at a
 * glance, that it clears the row's two lines of text rather than overlapping them,
 * and that both states carry a theme token for background AND text, so neither
 * disappears in dark mode.
 *
 * WHY ISOLATED: the picker is a portalled popover anchored to a measured rect inside
 * ChatPage, which needs the app shell, a live websocket and a seeded session to reach;
 * a half-stubbed shell renders its error boundary instead, and a screenshot of the
 * wrong thing is worse evidence than none. `anchorRect` supplies the measurement the
 * shell would.
 *
 * `theme` and `tab` come from the query string: ?theme=light&tab=recent. Three more
 * switches stage the states a test asserts but no other frame shows: `read=fail` rejects
 * the favourites read, `write=fail` refuses every star click the way the gateway refuses a
 * sensitive path, and `browse=fav` opens Browse on a directory that is already a favourite.
 */
import { createRoot } from 'react-dom/client'
import { useRef } from 'react'
import ProjectPicker from '../src/components/ProjectPicker'
import { api } from '../src/api/client'
// The tab labels and the empty state ARE catalog strings, so an uninitialised i18n
// would render them empty and the frame would document nothing.
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme)

const FAVORITES = ['/home/dev/projects/api-gateway', '/home/dev/projects/web-console']
const RECENTS = [
  '/home/dev/notes',
  '/home/dev/projects/api-gateway',
  '/home/dev/projects/billing-service',
]

// Stubbed on the client rather than through `fetch`, so the frame cannot depend on a
// gateway being up. An empty list is served when ?tab=empty, to capture the state a
// fresh install opens on.
const empty = params.get('tab') === 'empty'
const readFails = params.get('read') === 'fail'
const writeFails = params.get('write') === 'fail'
const browseFav = params.get('browse') === 'fav'
api.favoriteProjects = async () => {
  if (readFails) throw new Error('favourites read failed')
  return { dirs: empty ? [] : FAVORITES }
}
api.recentProjects = async () => ({ dirs: RECENTS })
api.browseDirs = async () => ({
  ...(browseFav
    ? { path: '/home/dev/projects/api-gateway', parent: '/home/dev/projects', dirs: [
        { name: 'src', path: '/home/dev/projects/api-gateway/src' },
        { name: 'test', path: '/home/dev/projects/api-gateway/test' },
      ] }
    : { path: '/home/dev/projects', parent: '/home/dev', dirs: [
        { name: 'api-gateway', path: '/home/dev/projects/api-gateway' },
        { name: 'web-console', path: '/home/dev/projects/web-console' },
      ] }),
})
api.addFavoriteProject = async (p: string) => {
  if (writeFails) throw new Error('Access denied')
  return { dirs: [...FAVORITES, p] }
}
api.removeFavoriteProject = async () => ({ dirs: [FAVORITES[0]] })

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
        notes
      </button>
      <ProjectPicker open onOpenChange={() => {}} anchorRef={anchor} onSelect={() => {}} />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
