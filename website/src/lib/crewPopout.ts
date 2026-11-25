/**
 * A Remote Crew pane's pop-out in the desktop app.
 *
 * The desktop shell opens an in-app window only for a page on the window's own
 * origin, and a pane runs on the crew's tunnel port. So the pane opens the HUB's
 * `/popout/crew` page, which frames the crew's own pop-out page the way the hub
 * frames the pane (`pages/CrewPopoutFrame.tsx`).
 */
import { withBase } from './basePath'

export const CREW_POPOUT_ROUTE = '/popout/crew'
/** Advertised as `crewPopout` in a host model by a hub that serves `CREW_POPOUT_ROUTE`. */
export const CREW_POPOUT_VERSION = 1

/** Pane side: the hub page that frames `popoutUrl`, a pop-out URL on this page's origin. */
export function crewPopoutUrl(hubOrigin: string, popoutUrl: string): string {
  const { pathname, search } = new URL(popoutUrl, window.location.href)
  const query = new URLSearchParams({ port: window.location.port, path: pathname + search })
  return `${hubOrigin}${CREW_POPOUT_ROUTE}?${query}`
}

/**
 * Hub side: the frame src for `path` on `crewOrigin`, carrying `token`, or null
 * when `path` resolves off `crewOrigin` or outside its pop-out pages.
 */
export function framedCrewPopoutSrc(crewOrigin: string, path: string, token: string): string | null {
  const url = new URL(path, crewOrigin)
  if (url.origin !== crewOrigin || !url.pathname.startsWith(withBase('/popout/'))) return null
  url.searchParams.set('token', token)
  return url.href
}
