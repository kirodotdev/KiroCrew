import { isTouchDevice } from '../../utils/isTouchDevice'

/**
 * Putting the caret in the chat composer after creating a session.
 *
 * The single-chat surface renders ONE composer bound to whichever slot is
 * currently active. That is what makes the ordering load-bearing: focusing the
 * composer while a `createSlot` is still in flight puts the caret on the OLD
 * session, so anything the user types in that window becomes the old slot's
 * draft and is lost the moment the new slot activates. Slow creation makes the
 * window real rather than theoretical.
 *
 * The session-grid split view breaks the one-composer assumption: each
 * `ChatPane` mounts its own composer, so N composers coexist and a
 * document-global first-match lookup would always land on the first pane.
 * `queryComposer` therefore scopes the lookup to the pane holding focus.
 */

/**
 * The one place the composer is looked up.
 *
 * Resolution order:
 *  1. The composer inside the pane that currently holds focus — the
 *     `[data-chat-pane]` ancestor of `document.activeElement`. In the split
 *     view every pane mounts its own composer, and a global shortcut must act
 *     on the pane the user is working in, not the first pane in document
 *     order.
 *  2. The composer inside the grid-focused pane (`[data-chat-pane="focused"]`).
 *     The pane's pickers render through portals under `document.body`, so
 *     while one is open the active element has NO pane ancestor — the grid's
 *     own focused-pane marker is what still names the pane the user is in.
 *  3. Document-wide fallback, which preserves single-pane behaviour: with one
 *     composer on the page (or focus outside any pane in a view with no
 *     focused marker) the first match IS the right one.
 *
 * The probe is the stable `data-composer-input` hook, NOT the textarea's
 * aria-label: the label is `i18nT('components.chatInput.message_input')` and
 * every catalog translates it, so a label-based selector matches in English
 * only and focus silently no-ops in the other eleven languages. The `data-`
 * attribute is invisible to assistive tech and never translated, which leaves
 * the label free to localize.
 */
export function queryComposer(): HTMLTextAreaElement | null {
  const pane =
    document.activeElement?.closest('[data-chat-pane]') ??
    document.querySelector('[data-chat-pane="focused"]')
  const scoped = pane?.querySelector<HTMLTextAreaElement>('textarea[data-composer-input]')
  return scoped ?? document.querySelector<HTMLTextAreaElement>('textarea[data-composer-input]')
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
