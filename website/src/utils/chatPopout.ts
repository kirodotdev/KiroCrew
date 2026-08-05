import { toSlug } from './shareUrl'
import {
  createPopoutController,
  applyMessage,
  pruneStale,
  HEARTBEAT_MS,
  STALE_MS,
  type PopoutMap,
  type PopoutMsg,
} from './popoutController'

/**
 * Cross-window coordination for popped-out chat sessions.
 *
 * A "pop out" opens a session in a dedicated same-origin browser window at
 * `/popout/chat/:slug?sid=<slotKey>`. The generic coordination engine lives in
 * `popoutController` (BroadcastChannel + visibility-aware heartbeat +
 * window-handle dedupe + popup-blocker observability); this module just
 * specializes it for chat sessions: the channel name, the URL shape, the
 * window name, and the `/chat` main-view fallback for `returnSelfToMain`.
 * The pure helpers are re-exported so tests keep importing them from here.
 */

export const CHAT_POPOUT_CHANNEL = 'kirocrew-chat-popout'

export { HEARTBEAT_MS, STALE_MS, applyMessage, pruneStale }
export type { PopoutMap, PopoutMsg }

/**
 * Stable, filesystem-safe window name for a session. `window.open` reuses (and
 * focuses) an existing window with the same name, giving dedupe for free even
 * after the opener lost its handle (e.g. a main-window refresh).
 */
export function popoutWindowName(slot: string): string {
  return `mc-popout-${slot.replace(/[^a-zA-Z0-9_-]/g, '_')}`
}

/** Build the popout URL for a session, mirroring the `/chat` share-link shape. */
export function buildPopoutUrl(slot: string, title?: string): string {
  const slug = title && title !== slot ? toSlug(title) : ''
  const params = new URLSearchParams({ sid: slot })
  return `${window.location.origin}/popout/chat${slug ? '/' + slug : ''}?${params.toString()}`
}

const controller = createPopoutController({
  channelName: CHAT_POPOUT_CHANNEL,
  logLabel: 'chatPopout',
  buildUrl: buildPopoutUrl,
  windowName: popoutWindowName,
  // returnSelfToMain fallback: become the main chat view for this session
  // (deep-linked / restored popouts have no script opener, so close is refused).
  mainViewUrl: slot => slot ? `/chat?${new URLSearchParams({ sid: slot })}` : '/chat',
})

/** Subscribe a main-window listener (for useSyncExternalStore). Starts the heartbeat lazily. */
export const subscribe = controller.subscribe
/** Current set of popped-out slot keys (stable identity until membership changes). */
export const getSnapshot = controller.getSnapshot
/** Open (or focus, if already open) a session in its own browser window. */
export const openPopout = controller.openPopout
/** Focus the popout window for a session (direct handle, else ask it to focus itself). */
export const focusPopout = controller.focusPopout
/** Close a session's popout window and drop it from the map (caller re-selects it in main). */
export const bringBack = controller.bringBack
/**
 * True when THIS window is the live popout for `slot`. Surfaces rendered
 * inside a popout (the header menu re-renders in there) need this: a popout
 * never holds its own slot in the coordination map (BroadcastChannel doesn't
 * self-deliver), so `isPoppedOut(ownSlot)` reads false in the popout window.
 */
export const isSelfPopout = controller.isSelfPopout
/**
 * Return THIS popout window's session to the main dashboard: focus the opener
 * and close; when the close is refused (no script opener), navigate this
 * window to the main chat view instead.
 */
export const returnSelfToMain = controller.returnSelfToMain
/** Register THIS window as the live popout for `slot` (responder role). Returns cleanup. */
export const registerPopout = controller.registerPopout
/** Test-only: swap the navigation sink (jsdom can't redefine window.location). */
export const __setNavigateForTests = controller.__setNavigateForTests
/** Test-only: reset all module state between cases. */
export const __resetForTests = controller.__resetForTests
