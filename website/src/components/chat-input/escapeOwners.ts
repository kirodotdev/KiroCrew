/* Shared Escape-precedence probe for the composer (issue #22315).
 *
 * The composer binds Escape to two things that must YIELD to anything already
 * holding Escape on the page: stopping the running turn (stopKey.ts) and
 * discarding a live/draining dictation (voice.ts). Both ask the same question
 * — "is some other surface the current Escape owner?" — so they ask it in ONE
 * place rather than each carrying its own copy of the selector (the drift a
 * second copy invites: one gets a carve-out, the other does not, and the two
 * disagree about who owns Escape).
 *
 * A surface counts as an Escape owner when it is open and will itself answer
 * Escape (close, dismiss, cancel). Two markers name them:
 *
 *   - `[role="dialog"]` — modal dialogs, the command bar, snip/confirm
 *     overlays. These bind Escape on `window` and own it over the composer;
 *     one presence check defers to every one of them rather than enumerating
 *     them by name.
 *
 *   - `[data-escape-owner]` — a NON-dialog surface that nonetheless answers
 *     Escape from its own document-level handler WITHOUT `role="dialog"` and
 *     WITHOUT `preventDefault` (so neither the dialog probe nor a
 *     `defaultPrevented` check would see it). The embedded file preview
 *     (MarkdownPanel) and the in-session message-search bar are the two such
 *     surfaces today: both close on Escape from a document listener that does
 *     not stop the event, so without this marker a single Escape would BOTH
 *     close the surface AND stop the turn / discard dictation. The attribute
 *     is set by the surface only while it actually owns Escape.
 *
 * Surfaces registered on the Escape stack (hooks/useEscapeLayer) are ordered
 * there instead and need no marker.
 *
 * The notification banner is deliberately NOT marked. Its window handler
 * already skips an Escape whose `defaultPrevented` is set, and both composer
 * claimants preventDefault the Escape they take before it bubbles to
 * document. So a focused composer keeps its own Escape (stop the turn,
 * discard the dictation) and the card stays; marking the banner would make
 * one pending card silently disable both bindings.
 *
 * `[data-escape-passthrough]` opts a surface OUT even though it carries
 * `role="dialog"`: ComputerUseLiveView is a strictly read-only panel open
 * ALONGSIDE a focused composer that never steals focus or claims Escape, so
 * the probe must not treat it as an owner (issue #22315, HOLD item 4).
 *
 * SessionControlHost deliberately stays in the `[role="dialog"]` set: it is a
 * non-modal dialog that does NOT steal focus and lets Escape reach the
 * document unstopped (its onKeyDown is `if (e.key !== 'Escape')
 * e.stopPropagation()`, and its document Escape listener closes without
 * preventDefault), so it relies on THIS probe to keep one Escape from both
 * closing it and stopping the turn.
 */

/** True when a surface other than the composer currently owns Escape. The
 *  selector is inlined (not an ALL-CAPS module constant) so the strict-i18n
 *  gate, which inspects ALL-CAPS constants for user copy, does not read this
 *  CSS selector as an untranslated string. */
export function anotherEscapeOwnerIsOpen(): boolean {
  return typeof document !== 'undefined'
    && document.querySelector('[role="dialog"]:not([data-escape-passthrough]), [data-escape-owner]') != null
}
