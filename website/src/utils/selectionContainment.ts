/**
 * Containment for a DOM selection, judged by text rather than by ancestry.
 *
 * `range.commonAncestorContainer` is the wrong predicate for a multi-click
 * (double/triple-click) selection of a container's FIRST or LAST block, because
 * browsers normalize such a selection to a boundary point just OUTSIDE the
 * container: a triple-click paragraph selection ends "at the start of the next
 * block", and for the last block that position lives in the container's PARENT.
 * The common ancestor is hoisted above the container even though every selected
 * character is inside it, so a bare `container.contains(range.commonAncestorContainer)`
 * early-return dismisses a selection that is entirely the container's own.
 *
 * That is what made the chat selection toolbar appear for click-drag but not for
 * double/triple-click on a message's last line (#7847), and the same predicate
 * carried the same defect at two more surfaces (#7891): copying a user bubble's
 * last line shipped the raw paste-chip label, and triple-clicking a spec
 * document's last paragraph raised no Comment pill.
 */

/**
 * The part of `range` that lies inside `container`, or `null` when the selection
 * is not the container's to act on.
 *
 * A returned range is always clamped to the container, so callers can measure,
 * stringify, or clone from it without an accepted overhang dragging the next
 * block's line box or nodes into the result. The fast path returns the original
 * range untouched, so the common case allocates nothing.
 *
 * Rejection has two tiers, and the order matters. The O(1) tier comes first:
 * where the caller listens on `document` one instance per message, the N-1
 * instances that do not own the selection must not fall through to
 * stringification, which would serialize text growing with transcript distance
 * on every event (select-all being the worst case). A boundary-normalized
 * multi-click always keeps at least one endpoint inside its own container, so
 * this tier never rejects the case the predicate exists for. Only then is the
 * text tier consulted: clamp a clone to each side of the container and require
 * both overhangs to hold no text, which keeps a selection genuinely spanning
 * into a sibling rejected in either direction.
 */
export function containedSelectionRange(range: Range, container: Node): Range | null {
  if (container.contains(range.commonAncestorContainer)) return range

  const startInside = container.contains(range.startContainer)
  const endInside = container.contains(range.endContainer)
  if (!startInside && !endInside) return null

  // `setEnd` before the start (or `setStart` after the end) collapses the clone
  // per the DOM spec, so the overhang on the contained side reads as empty.
  const before = range.cloneRange()
  before.setEnd(container, 0)
  const after = range.cloneRange()
  after.setStart(container, container.childNodes.length)
  if ((before.toString() + after.toString()).trim()) return null

  const clamped = range.cloneRange()
  if (!startInside) clamped.setStart(container, 0)
  if (!endInside) clamped.setEnd(container, container.childNodes.length)
  return clamped
}
