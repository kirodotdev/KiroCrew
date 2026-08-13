/**
 * Arrow-key roving for the sidebar session rows.
 *
 * Each keyboard-focusable session row carries `data-session-row="<slot key>"`
 * and `data-session-scope="<render scope>"`. The scope is what bounds a rove:
 * the sidebar renders the same slot in more than one place (the folder list,
 * the flat list, one board column per tag), so ArrowDown has to walk the list
 * the focused row actually sits in instead of jumping between them. Scope is
 * compared through `dataset` rather than baked into the selector because a
 * board scope is a column id, which is not guaranteed to be selector-safe.
 *
 * The rove moves FOCUS only — Enter/Space on a row still switches to it — so a
 * keyboard user can read down the list without loading every session on the way.
 */

/** Marks a row as a rove stop. Rows without it are skipped. */
export const SESSION_ROW_SELECTOR = '[data-session-row]'

/**
 * Every row rendered in the same scope as `row`, in DOM order, minus the ones
 * that cannot take focus.
 *
 * A collapsed folder still renders its rows — `FolderBody` hides them with
 * `inert` + `visibility: hidden` rather than unmounting, so the collapse can
 * animate to intrinsic height. Those rows are unfocusable, so leaving them in
 * would stall the rove on an invisible row instead of carrying on to the next
 * visible session. `row` itself is always kept so the caller's `indexOf` holds.
 */
export function sessionRowsInScope(row: HTMLElement): HTMLElement[] {
  const scope = row.dataset.sessionScope ?? ''
  const all = row.ownerDocument.querySelectorAll<HTMLElement>(SESSION_ROW_SELECTOR)
  return Array.from(all).filter(el =>
    (el.dataset.sessionScope ?? '') === scope && (el === row || el.closest('[inert]') === null))
}

/**
 * The row `step` places from `row` inside its scope, clamped at both ends, or
 * null when there is nowhere to go.
 *
 * Clamping rather than wrapping: this is a focus rove over a visible list, so
 * it follows the ARIA listbox default (and `useListKeyboardNav`'s `wrap: false`
 * default). Wrapping is what the ⌘[ / ⌘] and Alt+arrow session-cycle chords do,
 * and those are a different gesture — they switch the session from anywhere,
 * including the composer, so there is no list edge to stop at.
 */
export function siblingSessionRow(row: HTMLElement, step: number): HTMLElement | null {
  const rows = sessionRowsInScope(row)
  const cur = rows.indexOf(row)
  if (cur < 0) return null
  const next = cur + step
  if (next < 0 || next >= rows.length) return null
  return rows[next]
}

/**
 * Move focus to the neighbouring row. Returns true when focus moved, so the
 * caller only claims the keystroke (preventDefault) when there was somewhere to
 * go — at the list edge the arrow key falls through and still scrolls the list.
 */
export function focusSiblingSessionRow(row: HTMLElement, step: number): boolean {
  const target = siblingSessionRow(row, step)
  if (!target) return false
  target.focus()
  if (typeof target.scrollIntoView === 'function') target.scrollIntoView({ block: 'nearest' })
  return target.ownerDocument.activeElement === target
}
