import { isTouchDevice } from '../../utils/isTouchDevice'

/**
 * Putting the caret in the chat composer after creating a session.
 *
 * There is exactly ONE composer element on the page and it is bound to
 * whichever slot is currently active. That single fact is what makes the
 * ordering load-bearing: focusing the composer while a `createSlot` is still in
 * flight puts the caret on the OLD session, so anything the user types in that
 * window becomes the old slot's draft and is lost the moment the new slot
 * activates. Slow creation makes the window real rather than theoretical.
 *
 * Both New-chat entry points (the sidebar header button and the collapsed
 * sidebar's hover flyout) need the same sequence, and the selector was
 * previously spelled out at each call site.
 */

/**
 * Focus the composer on the next frame.
 *
 * Next frame, not synchronously: the caller has just changed store state, and
 * the composer for the newly active slot has not been committed to the DOM yet —
 * focusing now would either find the old element or nothing.
 *
 * Skipped on touch devices, where focusing a textarea raises the on-screen
 * keyboard and covers the thing the user just created.
 *
 * The selector is written inline rather than hoisted to a named constant on
 * purpose: the i18n lint exempts literals passed straight to `querySelector`
 * (a machine callee) but reports any named string constant, and
 * `aria-label="Message input"` reads as prose to it. Same spelling as the
 * keyboard-shortcut layer's lookup — keep them in step.
 */
export function focusComposer(): void {
  requestAnimationFrame(() => {
    if (isTouchDevice()) return
    document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message input"]')?.focus()
  })
}

/**
 * Focus the composer once `created` fulfils — never before.
 *
 * Rejection is swallowed on purpose: a failed create surfaces through the
 * store's own rejected handling, there is no new composer to focus, and an
 * unhandled rejection here would be reported as a page error.
 */
export function focusComposerAfter(created: Promise<unknown>): void {
  void created.then(focusComposer).catch(() => {})
}
