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
  if (scoped) return scoped
  /**
   * Document-wide fallback, EXCLUDING the side chat's own composer.
   *
   * The side chat is a separate conversation that mounts this same component, so
   * its textarea carries the same `data-composer-input` hook -- and it is never a
   * valid answer here: nothing in the side chat routes through this module (it owns
   * its own composer), while ChatPage's own comment on `handleAsk` records that
   * routing a selection to the side chat must happen "WITHOUT touching the main
   * chat context (unlike handleQuote, which injects into the main composer)". The
   * two are deliberately different destinations.
   *
   * A first-match fallback conflated them, and the main composer becoming
   * COLLAPSIBLE is what turned that latent conflation into a live one: while the
   * main composer is collapsed it is unmounted, so the side chat's textarea became
   * the only match and a main-chat intent resolved to it -- focus, and worse a
   * quote-to-compose or widget PRE-FILL, landing in a different conversation.
   * Review caught it. Skipping those candidates means a collapsed main composer
   * reports MISSING, which is what makes the expand request fire instead.
   *
   * `[data-side-chat-input]` is the marker ChatPage already uses to find that
   * composer (`handleAsk`'s mount probe), not one invented here.
   */
  const all = document.querySelectorAll<HTMLTextAreaElement>('textarea[data-composer-input]')
  for (const ta of all) {
    if (!ta.closest('[data-side-chat-input]')) return ta
  }
  return null
}

/**
 * "Someone wants the composer" — broadcast when a focus intent finds no composer.
 *
 * The composer can be collapsed for reading, in which case it is UNMOUNTED and
 * every lookup below returns null. A focus intent that just gave up there would
 * dead-end a deliberate gesture: `/`, quote-to-compose, a widget send and
 * post-create focus would all silently do nothing, and a pre-fill would land in a
 * draft behind the collapsed bar.
 *
 * An event rather than a call: this module is imported by page-level code, and the
 * collapse state belongs to the composer component. The listener lives there, so
 * neither side has to reach into the other, and a host with no collapsible
 * composer simply has no listener.
 */
export const COMPOSER_EXPAND_EVENT = 'mc-expand-composer'

/**
 * Ask any collapsed composer to come back. Returns whether one actually did.
 *
 * The return value is what keeps this from stealing focus. A retry scheduled on a
 * later frame outlives the intent that asked for it: the user may have clicked
 * elsewhere, or the slot may have changed, and focusing the composer then is
 * exactly the stolen-focus class `releaseComposerForKeyboardSwitch` exists to
 * prevent. The existing suite caught it -- a retry queued by one caller landed in
 * the next test and focused a composer nobody had asked for.
 *
 * So the event is CANCELABLE and the listener calls `preventDefault()` only when it
 * was really collapsed. No collapsed composer means no listener answers, the call
 * reports false, and nothing is scheduled -- so every path that was already
 * finding its composer, and every host with no collapsible composer at all, behaves
 * exactly as before.
 */
export function requestComposerExpand(): boolean {
  return !window.dispatchEvent(new Event(COMPOSER_EXPAND_EVENT, { cancelable: true }))
}

/**
 * Resolve the composer, asking a collapsed one to return and retrying once.
 *
 * Only a miss that an expand can actually fix pays anything. The retry is deferred
 * by a frame because the expand is a state change and React has to commit before
 * the textarea exists.
 *
 * Exported for the two callers that deliberately do NOT go through
 * `focusComposer` -- Alt+Enter ("focus text input") and the new-chat shortcut's
 * post-create focus. Both skip it for one reason: a pressed keyboard shortcut
 * proves a keyboard exists, so `focusComposer`'s touch-device skip would wrongly
 * suppress them on a tablet with a physical keyboard. That is a reason to skip the
 * TOUCH GUARD, not a reason to skip the collapsed-composer lookup, and calling
 * `queryComposer` directly left both as standing dead ends -- exactly the failure
 * this module fixes for `/`. Review found both. Note the common path stays
 * synchronous: when the composer is already there the callback runs before this
 * returns, so neither caller loses the ordering its own comment relies on.
 */
export function queryComposerOrExpand(then: (ta: HTMLTextAreaElement) => void): void {
  const ta = queryComposer()
  if (ta) { then(ta); return }
  if (!requestComposerExpand()) return
  requestAnimationFrame(() => {
    const revealed = queryComposer()
    if (revealed) then(revealed)
  })
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
    queryComposerOrExpand(ta => ta.focus())
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
    queryComposerOrExpand(ta => {
      if (isTouchDevice()) {
        if (typeof ta.scrollIntoView === 'function') ta.scrollIntoView({ block: 'nearest' })
      } else {
        ta.focus()
      }
    })
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

/**
 * One-shot "keyboard switch: leave the composer alone" signal.
 *
 * On macOS, letter jump chords are input-gated (Ctrl+A/E/K are Cocoa readline
 * bindings inside text fields; Option+letter composes characters), and every
 * session switch autofocuses the composer via ChatInput's autoFocusKey effect.
 * Together those made keyboard navigation self-terminating: jump once, focus
 * lands in the composer, and the next letter chord is dead until the user
 * clicks the chat to release focus.
 *
 * A keyboard-driven switch (jump digit/letter, ⌘brackets, Alt+arrows, MRU)
 * calls `releaseComposerForKeyboardSwitch()` on macOS: it blurs the composer
 * if the keystroke came from inside it, and arms this flag so the autofocus
 * effect skips exactly one key transition. Chords then chain indefinitely;
 * `/` (or a click) focuses the composer when the user wants to type. Pointer
 * switches never call this, so click-a-row still means type-immediately.
 *
 * A module-level flag, not store state: the producer (keydown handler) and
 * consumer (effect on the very next commit) are synchronous within one
 * switch, and routing it through the store would re-render every composer
 * for what is a single-frame handshake.
 */
let composerReleaseArmedAt = 0 // 0 = unarmed; else Date.now() at arming

/**
 * The legitimate consumer (ChatInput's autoFocusKey effect) runs in the same
 * commit as the switch dispatch — milliseconds. A flag older than this is by
 * definition leaked: some surface armed it with no mounted consumer whose
 * autoFocusKey transitions (e.g. split view, where panes bind a fixed slot
 * key and the top-level ChatInput is unmounted). Expiring it here closes the
 * whole no-consumer class instead of enumerating each such surface.
 */
const COMPOSER_RELEASE_TTL_MS = 1500

export function releaseComposerForKeyboardSwitch(): void {
  composerReleaseArmedAt = Date.now()
  const ae = document.activeElement
  if (ae instanceof HTMLTextAreaElement && ae.hasAttribute('data-composer-input')) ae.blur()
}

/** Consume the one-shot release. True = the autofocus effect must skip this transition. */
export function consumeComposerRelease(): boolean {
  const armedAt = composerReleaseArmedAt
  composerReleaseArmedAt = 0
  return armedAt !== 0 && Date.now() - armedAt < COMPOSER_RELEASE_TTL_MS
}
