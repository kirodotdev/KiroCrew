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
 * `theme` and `tab` come from the query string: ?theme=light&tab=recent
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

const FAVORITES = ['/local/home/jjaskula/devel/qdash', '/local/home/jjaskula/devel/kirocrew']
const RECENTS = [
  '/local/home/jjaskula/.kiro/crew/workspace',
  '/local/home/jjaskula/devel/qdash',
  '/local/home/jjaskula/devel/qsrlite',
]

// Stubbed on the client rather than through `fetch`, so the frame cannot depend on a
// gateway being up. An empty list is served when ?tab=empty, to capture the state a
// fresh install opens on.
const empty = params.get('tab') === 'empty'
api.favoriteProjects = async () => ({ dirs: empty ? [] : FAVORITES })
api.recentProjects = async () => ({ dirs: RECENTS })
api.browseDirs = async () => ({
  path: '/local/home/jjaskula/devel',
  parent: '/local/home/jjaskula',
  dirs: [
    { name: 'kirocrew', path: '/local/home/jjaskula/devel/kirocrew' },
    { name: 'qdash', path: '/local/home/jjaskula/devel/qdash' },
  ],
})
api.addFavoriteProject = async (p: string) => ({ dirs: [...FAVORITES, p] })
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
        workspace
      </button>
      <ProjectPicker open onOpenChange={() => {}} anchorRef={anchor} onSelect={() => {}} />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
