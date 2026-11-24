/**
 * The pages a person can open in the workspace: the one table. The palette on the palm (`Palette.tsx`) and the
 * list behind the plus in the flat preview's bar (`OpenPage.tsx`) both read it, so the list is changed here
 * and nowhere else. The palm holds the short list (`PAGE_MENU`); the plus holds it too while the gateway's panels
 * are guarded, and the dashboard's own navigation while they are open (`pagesToOpen`).
 *
 * Each row is a page of the dashboard with the dashboard's own icon and its own words: the rail's label where the
 * rail has the page (`surfaces/builtins.tsx`), else the page's own title. Two rows need a word: the rail calls
 * `/chat` "Sessions", which is also what the list of all sessions is, so the chat carries the name of its own tab,
 * and `/sessions` its page title. Every path here is one that a panel may hold (`streamPath.ts`); a test says so.
 */
import { Bell, CalendarDays, Component, MessageSquare, MessagesSquare, Settings } from 'lucide-react'
import type { ReactElement } from 'react'
import AppIcon from '../../../components/AppIcon'
import { CrewMemberMark } from '../../../components/CrewMemberMark'
// Side effect: the dashboard's own pages are registered before the list below reads them.
import '../../../surfaces/builtins'
import { getAdvertisedSurfaces, surfaceLabel, type Surface } from '../../../surfaces/registry'
import { surfaceSwitchedOn } from '../../../surfaces/surfaceData'
import type { PanelWay } from './panelWay'
import { livePath } from './streamPath'

export interface MenuPage {
  /** The page's path on the dashboard. */
  path: string
  /** The catalog key of the dashboard's own label for it. Left out by a page of the navigation that has none. */
  labelKey?: string
  /** The page's label as the navigation registered it, in English: what shows when there is no catalog key. */
  label?: string
  /** The dashboard's own icon for it, at the size of the rail's. */
  icon: ReactElement
}

/** A page's name as the list shows it, in the language of this render. */
export const menuLabel = (page: MenuPage): string => surfaceLabel({ label: page.label ?? '', labelKey: page.labelKey })

export const PAGE_MENU: readonly MenuPage[] = [
  { path: '/chat', labelKey: 'app.tab_title_chat', icon: <MessageSquare size={16} /> },
  { path: '/members', labelKey: 'nav.crew_members', icon: <CrewMemberMark /> },
  { path: '/artifacts', labelKey: 'nav.artifacts', icon: <Component size={16} /> },
  { path: '/schedule', labelKey: 'nav.schedule', icon: <CalendarDays size={16} /> },
  { path: '/projects', labelKey: 'nav.task_runner', icon: <AppIcon iconUrl="/app-assets/projects/icon.svg" icon="ClipboardCheck" size={20} /> },
  { path: '/sessions', labelKey: 'pages.sessionsPage.page_title', icon: <MessagesSquare size={16} /> },
  { path: '/notifications', labelKey: 'nav.notifications', icon: <Bell size={16} /> },
  { path: '/settings', labelKey: 'nav.settings', icon: <Settings size={16} /> },
]

/**
 * The dashboard's own redirects (`App.tsx`, a `<Route>` whose element is a `<Navigate>`): a row whose path goes on to
 * another is listed only when that one may stand on a panel too. `pageMenu.test.tsx` reads `App.tsx` and keeps this
 * table equal to it.
 */
export const REDIRECTS: Readonly<Record<string, string>> = {
  '/knowledge': '/capabilities?tab=knowledge',
  '/overview': '/settings/overview',
  '/agents': '/capabilities',
  '/mc-agents': '/capabilities',
  '/instances': '/settings/instances',
  '/artifacts/deploy': '/deploy',
}

/** Whether a page of the navigation may stand on a panel: its path, and the path that it goes on to (its query aside). */
export function opensOnPanel(route: string): boolean {
  if (livePath(route) !== route) return false
  const to = REDIRECTS[route]
  return to === undefined || livePath(to.split('?')[0]) !== null
}

/** What the list reads of a page of the dashboard's navigation (`surfaces/registry.ts`, `Surface`). */
type NavPage = Pick<Surface, 'route' | 'label' | 'labelKey' | 'icon' | 'configSwitch' | 'configSwitchDefault'>

/**
 * The pages that the plus lists (`OpenPage.tsx`).
 *
 * With the gateway's panels open (`panelWay.ts`) nearly every page of the dashboard may stand on a panel, so the
 * list is the dashboard's own: every page that its navigation advertises now (`getAdvertisedSurfaces`), by the
 * navigation's own name and icon and in its own order, so that a page the dashboard gains shows up here with no
 * change of ours. A page is left out when its setting has it switched off, as the rail leaves it out, and when a
 * panel may not hold it or the page that it goes on to (`opensOnPanel`: the few pages that stay shut in both ways,
 * an address with a query, which is how the navigation names a tab of a page, and a row that is no route of this
 * origin). After them stand the rows of the short list that the navigation
 * does not name. shortcut: the pages of installed apps are not listed, the shell builds their rows in its own
 * state; upgrade when the shell shares that list.
 *
 * With the panels guarded, and until the gateway has said which way holds, it is the short list: each of its pages
 * opens in both ways.
 */
export function pagesToOpen(way: PanelWay, config: unknown, named: readonly NavPage[] = getAdvertisedSurfaces()): readonly MenuPage[] {
  if (way !== 'open') return PAGE_MENU
  const own = named
    .filter(s => surfaceSwitchedOn(s, config) && opensOnPanel(s.route))
    .map(s => ({ path: s.route, labelKey: s.labelKey, label: s.label, icon: s.icon }))
  // A page that the navigation names and leaves out now does not come back by the short list.
  const names = new Set(named.map(s => s.route))
  return [...own, ...PAGE_MENU.filter(page => !names.has(page.path))]
}
