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
 */

/**
 * The one place the composer is looked up.
 *
 * The probe is the stable `data-composer-input` hook, NOT the textarea's
 * aria-label: the label is `i18nT('components.chatInput.message_input')` and
 * every catalog translates it, so a label-based selector matches in English
 * only and focus silently no-ops in the other eleven languages. The `data-`
 * attribute is invisible to assistive tech and never translated, which leaves
 * the label free to localize.
 */
export function queryComposer(): HTMLTextAreaElement | null {
  return document.querySelector<HTMLTextAreaElement>('textarea[data-composer-input]')
}

/**
 * Focus the composer on the next frame.
 *
 * Next frame, not synchronously: the caller has just changed store state, and
 * the composer for the newly active slot has not been committed to the DOM yet —
 * focusing now would either find the old element or nothing.
 *
 * Skipped on touch devices, where focusing a textarea raises the on-screen
 * keyboard and covers the thing the user just created.
 */
export function focusComposer(): void {
  requestAnimationFrame(() => {
    if (isTouchDevice()) return
    queryComposer()?.focus()
  })
}

/**
 * Reveal the composer after pre-filling it (widget send, quote-to-compose).
 *
 * Touch devices scroll it into view WITHOUT focusing — focus would pop the
 * soft keyboard over the content the user was reading. Desktop focuses, which
 * scrolls it into view anyway. The `scrollIntoView` feature check keeps this
 * safe in DOM environments that do not implement it.
 */
export function revealComposer(): void {
  requestAnimationFrame(() => {
    const ta = queryComposer()
    if (!ta) return
    if (isTouchDevice()) {
      if (typeof ta.scrollIntoView === 'function') ta.scrollIntoView({ block: 'nearest' })
    } else {
      ta.focus()
    }
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
