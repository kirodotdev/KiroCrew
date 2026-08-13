import {
  createPopoutController,
  applyMessage,
  pruneStale,
  HEARTBEAT_MS,
  STALE_MS,
  NAV_CLAIM_MS,
  type PopoutMap,
  type PopoutMsg,
  type NavIntent,
} from './popoutController'

/**
 * Cross-window coordination for popped-out artifacts.
 *
 * A "pop out" opens an artifact in a dedicated same-origin browser window at
 * `/popout/artifact/:slug`. Unlike chat sessions (whose slot key isn't
 * URL-safe, so it rides in `?sid=`), an artifact's slug IS its URL identity, so
 * it's the route param directly. The generic coordination engine lives in
 * `popoutController`; this module specializes it for artifacts: a distinct
 * BroadcastChannel (no cross-talk with chat popouts), the `/popout/artifact/…`
 * URL, the window name, and the `/artifacts/<slug>` main-view fallback for
 * `returnSelfToMain`. Pure helpers are re-exported for unit tests.
 */

export const ARTIFACT_POPOUT_CHANNEL = 'kirocrew-artifact-popout'

export { HEARTBEAT_MS, STALE_MS, NAV_CLAIM_MS, applyMessage, pruneStale }
export type { PopoutMap, PopoutMsg, NavIntent }

/**
 * Stable, filesystem-safe window name for an artifact. `window.open` reuses (and
 * focuses) an existing window with the same name, giving dedupe for free even
 * after the opener lost its handle (e.g. a main-window refresh).
 */
export function popoutWindowName(slug: string): string {
  return `mc-artifact-popout-${slug.replace(/[^a-zA-Z0-9_-]/g, '_')}`
}

/** Build the popout URL for an artifact. The slug is URL-safe, so it's the path directly. */
export function buildPopoutUrl(slug: string): string {
  return `${window.location.origin}/popout/artifact/${encodeURIComponent(slug)}`
}

const controller = createPopoutController({
  channelName: ARTIFACT_POPOUT_CHANNEL,
  logLabel: 'artifactPopout',
  buildUrl: buildPopoutUrl,
  windowName: popoutWindowName,
  // returnSelfToMain fallback: become the main artifact detail view
  // (deep-linked / restored popouts have no script opener, so close is refused).
  mainViewUrl: slug => slug ? `/artifacts/${encodeURIComponent(slug)}` : '/artifacts',
})

/** Subscribe a main-window listener (for useSyncExternalStore). Starts the heartbeat lazily. */
export const subscribe = controller.subscribe
/** Current set of popped-out artifact slugs (stable identity until membership changes). */
export const getSnapshot = controller.getSnapshot
/** Open (or focus, if already open) an artifact in its own browser window. */
export const openPopout = controller.openPopout
/** Focus the popout window for an artifact (direct handle, else ask it to focus itself). */
export const focusPopout = controller.focusPopout
/** Close an artifact's popout window and drop it from the map (caller re-views it in main). */
export const bringBack = controller.bringBack
/** True when THIS window is the live popout for `slug`. */
export const isSelfPopout = controller.isSelfPopout
/**
 * Return THIS popout window's artifact to the main dashboard: focus the opener
 * and close; when the close is refused (no script opener), navigate this
 * window to the artifact's detail page instead.
 */
export const returnSelfToMain = controller.returnSelfToMain
/** Register THIS window as the live popout for `slug` (responder role). Returns cleanup. */
export const registerPopout = controller.registerPopout
/**
 * From inside an artifact popout: forward a navigation intent (open a chat
 * session, go to the library, …) to a main dashboard window instead of
 * navigating locally — the popout window stays pinned to its artifact. Falls
 * back to a new browser tab when no main window claims it.
 */
export const forwardToMain = controller.forwardToMain
/**
 * Register THIS window as the performer of forwarded navigation intents
 * (main dashboard role — App.tsx's non-popout shell). Returns cleanup.
 */
export const setNavIntentHandler = controller.setNavIntentHandler
/** Test-only: swap the navigation sink (jsdom can't redefine window.location). */
export const __setNavigateForTests = controller.__setNavigateForTests
/** Test-only: swap the new-tab sink (asserts the no-main fallback). */
export const __setWindowOpenForTests = controller.__setWindowOpenForTests
/** Test-only: reset all module state between cases. */
export const __resetForTests = controller.__resetForTests
