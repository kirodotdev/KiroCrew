import { withBase } from '../lib/basePath'
import { createPopoutController } from './popoutController'
import { appPopoutPath, appPopoutRouteOf } from './appPopoutRoute'

/**
 * Cross-window coordination for popped-out app pages.
 *
 * A "pop out" opens an app (a builtin like `/dev-fleet`, or an installed one at
 * `/apps/<name>`) in a dedicated same-origin browser window under
 * `/popout/app/<route>`, so the user can keep chatting in the main window while
 * the app stays on screen. The window is its own JS context with its own store
 * and socket — the gateway stays the single source of truth, exactly as for
 * chat and artifact pop-outs. The generic engine lives in `popoutController`;
 * this module specializes it for apps: a distinct BroadcastChannel (no
 * cross-talk with the other pop-out features), the URL shape, the window name,
 * and the app's own dashboard route as the `returnSelfToMain` fallback.
 *
 * The entity id is the app's dashboard ROUTE (`/dev-fleet`, `/apps/foo`),
 * derived by `appPopoutRoute.ts`: it is the one identity the rail, the
 * palette and the URL already agree on, and it is what the pop-out window's
 * router basename is cut from.
 */

export const APP_POPOUT_CHANNEL = 'kirocrew-app-popout'

/**
 * Stable, filesystem-safe window name for an app route. `window.open` reuses
 * (and focuses) an existing window with the same name, giving dedupe for free
 * even after the opener lost its handle (e.g. a main-window refresh).
 */
export function popoutWindowName(route: string): string {
  return `mc-app-popout-${route.replace(/[^a-zA-Z0-9_-]/g, '_')}`
}

/**
 * Build the pop-out URL for an app route. Through `withBase`, so a sub-path
 * build (`KIROCREW_BASE_PATH`) opens the window under its mount too.
 *
 * `initialPath` is the app-space path and query the window opens at (the page
 * the user is on, `/aws-control/usage?tab=x`); without it the window opens at
 * the app's root. A hash is dropped, and a path the window would not pin to
 * `route` falls back to the root, so the window always registers under the
 * route the opener keyed it by.
 */
export function buildPopoutUrl(route: string, initialPath?: string): string {
  let start = route
  if (initialPath) {
    const path = initialPath.split('#')[0]
    const pathname = path.split('?')[0]
    if (appPopoutRouteOf(appPopoutPath(pathname)) === route) start = path
  }
  return `${window.location.origin}${withBase(appPopoutPath(start))}`
}

const controller = createPopoutController({
  channelName: APP_POPOUT_CHANNEL,
  logLabel: 'appPopout',
  // The controller's generic second `buildUrl` argument (a title for the chat
  // and artifact pop-outs) carries the initial path here.
  buildUrl: buildPopoutUrl,
  windowName: popoutWindowName,
  // returnSelfToMain fallback: become the main dashboard view of this app
  // (deep-linked / restored pop-outs have no script opener, so close is refused).
  mainViewUrl: route => route ?? '/apps',
})

/** Subscribe a main-window listener (for useSyncExternalStore). Starts the heartbeat lazily. */
export const subscribe = controller.subscribe
/** Current set of popped-out app routes (stable identity until membership changes). */
export const getSnapshot = controller.getSnapshot
/**
 * Open an app in its own browser window at `initialPath` (see
 * {@link buildPopoutUrl}). Keyed by `route`: while this window holds the
 * handle of that app's open window, it focuses that window and does not
 * navigate it, whatever `initialPath` says. Without the handle (this window
 * reloaded), `window.open` finds the window by its route-derived name and
 * loads the URL in it, as it always has.
 */
export const openPopout: (route: string, initialPath?: string) => void = controller.openPopout
/** Focus the pop-out window for an app (direct handle, else ask it to focus itself). */
export const focusPopout = controller.focusPopout
/**
 * Return THIS pop-out window's app to the main dashboard: focus the opener and
 * close; when the close is refused (no script opener), navigate this window to
 * the app's main dashboard page instead.
 */
export const returnSelfToMain = controller.returnSelfToMain
/** Register THIS window as the live pop-out for `route` (responder role). Returns cleanup. */
export const registerPopout = controller.registerPopout
/**
 * From inside an app pop-out: forward a navigation that LEAVES the app (a "go
 * to chat" button, a link to another page) to a main dashboard window instead
 * of mounting that page here — the pop-out window stays pinned to its app.
 * Falls back to a new browser tab when no main window claims it.
 */
export const forwardToMain = controller.forwardToMain
/**
 * Register THIS window as the performer of forwarded navigation intents
 * (main dashboard role — the non-popout shell). Returns cleanup.
 */
export const setNavIntentHandler = controller.setNavIntentHandler
/** Test-only: swap the navigation sink (jsdom can't redefine window.location). */
export const __setNavigateForTests = controller.__setNavigateForTests
/** Test-only: swap the new-tab sink (asserts the no-main fallback). */
export const __setWindowOpenForTests = controller.__setWindowOpenForTests
/** Test-only: reset all module state between cases. */
export const __resetForTests = controller.__resetForTests
