import { createPopoutController } from './popoutController'
import { appPathname, withBase } from '../lib/basePath'

/**
 * A side-panel file in its own window (`/popout/file?path=…&sid=…`), so it is
 * not held to the panel's width. The window reads and saves the file itself
 * (`pages/FilePopoutFrame.tsx`), so of the shared controller this uses the
 * opener (the window size, the refusal in a desktop crew pane, the
 * blocked-pop-up notice, focusing a window this one already opened for the
 * same file), the hand-off of a navigation to the main dashboard, and closing
 * the window itself.
 */

/** The window's query string, which is also its id: the file, and the chat it
 *  was opened for (`sid`), so the gateway judges the read and the save for that
 *  chat exactly as it judges the side panel's. */
export function filePopoutQuery(path: string, slot?: string | null): string {
  return new URLSearchParams(slot ? { path, sid: slot } : { path }).toString()
}

const controller = createPopoutController({
  channelName: 'kirocrew-file-popout',
  logLabel: 'filePopout',
  buildUrl: query => `${window.location.origin}${withBase('/popout/file')}?${query}`,
  // One name per query: the query is percent-encoded ASCII, so every other
  // character, `_` included, maps to `_` and two hex digits.
  windowName: query => `mc-file-popout-${query.replace(/[^a-zA-Z0-9-]/g, c => '_' + c.charCodeAt(0).toString(16).padStart(2, '0'))}`,
  // Where `closeSelf` sends a window the browser will not let a script close.
  mainViewUrl: () => '/chat',
})

/** In a file pop-out window, the chat it was opened for (`sid`); null in any
 *  other window. That window has no active chat, so the viewer's
 *  session-scoped actions (artifact add and pin, an app's file-menu row) name
 *  this chat instead, and a restricted chat stays restricted there. */
export function filePopoutSlot(): string | null {
  return appPathname() === '/popout/file' ? new URLSearchParams(window.location.search).get('sid') : null
}

/** Open a file in its own window, or focus the one this window opened for it. */
export function openFilePopout(path: string, slot?: string | null): void {
  controller.openPopout(filePopoutQuery(path, slot))
}

/** From inside the pop-out: perform a navigation in the main dashboard instead. */
export const forwardToMain = controller.forwardToMain
/** The main dashboard's side of {@link forwardToMain} (registered by the app shell). */
export const setNavIntentHandler = controller.setNavIntentHandler
/** From inside the pop-out: close it, or become the main dashboard when the browser refuses. */
export const closeSelf = controller.closeSelf
