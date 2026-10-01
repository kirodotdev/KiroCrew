import { isChatPageSurface } from '../../utils/channelOrigin'
import { activeElementIsEditable } from '../../utils/editableTarget'
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
 *
 * Steps 1 and 2 are `focusedPane()` below, shared with the pending-approval
 * lookup so both chords agree about which pane they are in.
 */
export function queryComposer(): HTMLTextAreaElement | null {
  const pane = focusedPane()
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
 * Steps 1 and 2 of the resolution order documented on `queryComposer` — the pane
 * a global chord should act inside.
 *
 * ONE function rather than the same two selectors written out at each lookup:
 * every global chord that reaches into a pane has to agree about WHICH pane, and
 * two copies of that rule are two things to keep in step the next time the grid
 * changes how it marks the focused cell.
 */
function focusedPane(): Element | null {
  return document.activeElement?.closest('[data-chat-pane]') ??
    document.querySelector('[data-chat-pane="focused"]')
}

/**
 * The control a keyboard chord should land on when a tool call is waiting for a
 * decision, or null when nothing is waiting in the pane the user is working in.
 *
 * Returns the FIRST ENABLED button of the approval bar's own action row, so this
 * encodes no opinion about which decision is the default one: it lands where the
 * row already puts its first control, which is where a user tabbing backwards
 * from the composer arrives today. The chord removes keystrokes from a path that
 * already exists; it does not choose a verb.
 *
 * NOTHING IS RESOLVED BY LOOKING THIS UP. The caller focuses the element and
 * stops, so the keystroke cannot answer a prompt the reader has not read — the
 * decision still costs a second, deliberate press on a control that is now
 * visibly focused. That is the whole reason this is a focus lookup and not an
 * approve/reject action.
 *
 * Absence is the visibility guarantee, and it is structural rather than
 * enforced: the bar is only in the DOM while its slot holds an unresolved
 * approval, and a surface that renders no approval chrome (`SideChat` passes
 * `slotApprovalChrome={false}`) has no action row for this to find. No bar, no
 * element, and the chord is a no-op.
 *
 * NO CROSS-PANE FALLBACK, and this is where the contract departs from
 * `queryComposer`'s. That one may fall through to a document-wide match because
 * a pane without a composer is a case that does not arise; a pane without a
 * pending approval is the NORMAL case, and falling through there would focus a
 * control belonging to a session the user is not looking at. So the document-wide
 * branch is reached only when NO pane is resolved at all, which is the single-pane
 * page -- it mounts no `[data-chat-pane]`.
 *
 * Probed by `data-approval-actions`, not by button text or aria-label, for the
 * reason spelled out on `queryComposer`: every catalog translates those, so a
 * text-based selector would work in English and silently no-op in the other
 * eleven languages.
 */
export function queryPendingApprovalAction(): HTMLElement | null {
  const sel = '[data-approval-actions] button:not([disabled])'
  const pane = focusedPane()
  return pane
    ? pane.querySelector<HTMLElement>(sel)
    : document.querySelector<HTMLElement>(sel)
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
 * The slice of the store the quick-search helpers read: which slot is active,
 * and which `switchSlot` currently owns the claim on it (`pending` takes the
 * claim, the owning `fulfilled` or `rejected` clears it). Structural, so the
 * callers pass the app store (`useAppStore()`) and a test passes a stub, and so
 * this module stays a page-level import with no dependency on the store's own
 * types. `subscribe` is the plain Redux one: it returns the unsubscribe.
 */
export interface ActiveSlotStore {
  getState(): { chat: { activeSlot: string | null; slotSwitchRequestId: string | null; slotSwitchTarget: string | null } }
  subscribe(listener: () => void): () => void
}

/**
 * Put the caret in the composer after a quick-search surface -- the Cmd/Ctrl+K
 * Command Bar, or the legacy palette it falls back to -- opened a session
 * (#15732). The outcome a sidebar click already has.
 *
 * Why those surfaces cannot lean on ChatInput's autoFocusKey effect the way the
 * sidebar does: that effect fires on a slot-key TRANSITION, and declines without
 * retrying while an editable element holds focus. A quick-search surface hits
 * both gaps. Opening the session that is already active -- its row is one
 * keystroke away on both surfaces, and the search path resumes it through the
 * same async thunk -- changes nothing the effect watches, so it never runs. And
 * a live row's `switchSlot` moves the key synchronously while the surface is
 * still mounted with its own input focused (the Command Bar closes only once
 * the row's promise settles), so the effect sees an editable active element,
 * declines, and the one chance is spent.
 *
 * So the surface says what it means, and says it the way `useMessageSearch`
 * does when its bar closes -- but only once `switched`, the unwrapped
 * `switchSlot` dispatch, has FULFILLED, and only while `key` is still the
 * active slot. The ordering is the one this file's header states for a create,
 * read in the other direction: `switchSlot.pending` enters the target
 * synchronously, so the composer already answers to the target's key while the
 * gateway round trip is in flight. Focusing it then would route every
 * keystroke typed in that window to the TARGET's draft -- and a switch can
 * fail. A 404 unwinds the selection to the origin and evicts the gone row, and
 * the page files the text typed meanwhile under the evicted key, where no row
 * can ever show it again. So the caret moves only after the switch has landed:
 * a rejected switch gets no focus from here -- the selection unwinds to the
 * origin and the pane notice explains the dead gesture, and whatever the
 * composer's own autofocus does with that key transition is the sidebar's rule,
 * unchanged. The same bar is applied once
 * more ON the frame, through `store`: a fulfilment the user has already moved
 * past (a second gesture during the round trip) focuses nothing, matching the
 * `switchSlot.fulfilled` reducer, which ignores a payload for a slot that is
 * not the active one.
 *
 * One more case the frame has to read: two switches to the SAME key can
 * overlap -- a second gesture during the round trip, or the chat page's own
 * mount-time `switchSlot(activeSlot)` when the surface was used from another
 * page. The older read may land first. The slot is active, so the check above
 * passes, but the NEWER request owns the claim (`slotSwitchRequestId` with
 * `slotSwitchTarget === key`) and may yet unwind the selection with a 404 of
 * its own, so a caret placed now would route keystrokes to a slot about to be
 * evicted. The frame therefore defers: one store subscription, released the
 * first time the claim is no longer a pending same-key one. Cleared with the
 * slot still active (the newer read fulfilled) -> the focus proceeds, on a
 * fresh frame. Cleared with another slot active (the newer read 404ed and the
 * reducer unwound) -> nothing. "Focus only while no switch is pending" would
 * NOT do here: the mount-time duplicate has no focus helper of its own, so
 * giving up would leave the caret nowhere on every open from another page.
 *
 * `store` is a store HANDLE, not a value captured at the gesture: the surface
 * has closed and unmounted by the time the switch settles, so only the store
 * can still answer which slot is active (the sidebar reads live values the
 * same way, through `useStore().getState()`).
 *
 * The sidebar's rules carry over on purpose, all three. Touch devices are
 * skipped (an on-screen keyboard over the transcript the user just opened), and
 * a collapsed composer STAYS collapsed -- `queryComposer` reports it missing and
 * no expand is requested -- because opening a session is navigation, not the
 * typing intent `focusComposer` expands for, and the autoFocusKey effect
 * leaves a reading preference alone for the same reason. And the caret is not
 * taken from an editable element that holds focus ON the frame. The surfaces
 * themselves never leave one focused: the legacy palette restores nothing on
 * close, and the Command Bar's focus trap captures its own `autoFocus` input
 * (React applies `autoFocus` in the commit, before the trap's passive effect
 * reads `document.activeElement`), so its unmount restore reaches a detached
 * node and focus ends on `<body>`. An editable element focused by the time the
 * switch lands is therefore one the user chose during the round trip -- the
 * sidebar's search box, a title editor, the bar opened again -- and a late
 * caret must not yank them out of it.
 *
 * Not a one-shot for the effect to consume: in the same-key case the effect
 * does not run at all, so a flag would need its own re-render to be read, and
 * in the other case the surface is still open when it would run. Not a place
 * the macOS chord policy applies either: `releaseComposerForKeyboardSwitch`
 * keeps jump chords CHAINABLE, while a surface's Enter is the terminal pick of
 * a selection that closes the surface, so an unfocused composer there buys the
 * user nothing but a click.
 *
 * Rejection is swallowed on purpose, as `focusComposerAfter` does: the slice
 * records the failure and raises the notice, and an unhandled rejection here
 * would be reported as a page error.
 */
export function focusComposerForOpenedSession(switched: Promise<unknown>, key: string, store: ActiveSlotStore): void {
  void switched
    .then(() => requestAnimationFrame(() => focusOnceSwitchHasLanded(key, store)))
    .catch(() => {})
}

/**
 * The frame step of `focusComposerForOpenedSession`, re-entered on a fresh
 * frame after a deferral: read the store NOW -- the last moment before the
 * focus moves -- and act on what it says.
 */
function focusOnceSwitchHasLanded(key: string, store: ActiveSlotStore): void {
  const chat = store.getState().chat
  if (chat.activeSlot !== key) return
  if (chat.slotSwitchRequestId !== null && chat.slotSwitchTarget === key) {
    // A newer same-key switch owns the claim: wait for it to settle (see the
    // helper's comment). Released on the first store write that leaves the
    // claim no longer a pending same-key one; `unsubscribe` is safe to call
    // from inside the listener, Redux tolerates it.
    const unsubscribe = store.subscribe(() => {
      const now = store.getState().chat
      if (now.activeSlot === key && now.slotSwitchRequestId !== null && now.slotSwitchTarget === key) return
      unsubscribe()
      // Unwound to another slot (a 404), or the user moved on: not this gesture's
      // caret to place.
      if (now.activeSlot !== key) return
      // The store changed inside a dispatch; the DOM for it commits later. A
      // fresh frame re-reads everything, including a claim taken meanwhile.
      requestAnimationFrame(() => focusOnceSwitchHasLanded(key, store))
    })
    return
  }
  focusComposerNow()
}

/** The one place both quick-search helpers put the caret: the sidebar's three
 *  rules (touch, a field the user holds, and -- through `queryComposer` -- a
 *  collapsed composer stays collapsed, reported missing with no expand
 *  requested), then the composer.
 *
 *  Split view is skipped entirely. While a session-grid pane is mounted the
 *  page shows N composers, each bound to its own pane's slot, and the grid's
 *  focus model never follows `activeSlot` (see SessionGridView), so
 *  `queryComposer` would answer with the grid-focused pane's composer -- a
 *  session the gesture did not open, where the next Enter would send. A sidebar
 *  click leaves the split before it focuses; a quick-search open does not, so
 *  the honest answer here is no caret, exactly what the surfaces did before
 *  they said anything about focus. A lookup that resolves the pane bound to
 *  the opened key is the follow-up; nothing in the DOM names a pane's slot
 *  today. */
function focusComposerNow(): void {
  if (isTouchDevice()) return
  if (document.querySelector('[data-chat-pane]')) return
  if (activeElementIsEditable()) return
  queryComposer()?.focus()
}

/**
 * The same, once `resumed` -- an unwrapped `resumeFromHistory` dispatch -- has
 * actually entered the session.
 *
 * Settling is not enough here, which is why this is not `focusComposerAfter`:
 * the thunk FULFILS for a resume the chat page cannot display (an `ok: false`
 * answer, or a surface outside `isChatPageSurface`), and the reducer deliberately
 * leaves the active slot where it was in that case (#3624, #5925). Focusing
 * then would put the caret into the session the user was LEAVING while the
 * notice says the one they asked for did not open. The predicate is the
 * reducer's own. A rejected resume focuses nothing, for the reason
 * `focusComposerAfter` gives: the slice records it, and there is no new
 * composer to focus.
 *
 * No still-active read here: `resumeFromHistory` moves the active slot only in
 * its fulfilled reducer, at the moment this promise settles, so there is no
 * provisional window for a keystroke to land in, and the slot the reducer
 * entered IS the one the gesture named.
 */
export function focusComposerForResumedSession(resumed: Promise<{ ok: boolean; surface?: string }>): void {
  void resumed
    .then(result => { if (result.ok && isChatPageSurface(result.surface)) requestAnimationFrame(focusComposerNow) })
    .catch(() => {})
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
