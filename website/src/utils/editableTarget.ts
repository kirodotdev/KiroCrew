/** `Node.ELEMENT_NODE`, spelled out so a predicate stays usable for a plain
 *  object stand-in and never depends on a live `Node` binding. */
const ELEMENT_NODE = 1

/** Narrow a node to an element without requiring a live `Element` binding. */
function asElement(node: EventTarget | null | undefined): HTMLElement | null {
  const el = node as HTMLElement | null
  if (!el || el.nodeType !== ELEMENT_NODE || typeof el.tagName !== 'string') return null
  return el
}

/** True when this element consumes a printable keystroke, so a global hotkey
 *  must not claim it. `SELECT` counts: a printable key there is option
 *  typeahead, which the user is relying on. */
export function isEditableElement(node: EventTarget | null | undefined): boolean {
  const el = asElement(node)
  if (!el) return false
  const tag = el.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return true
  return el.isContentEditable === true
}

/** True when this element takes CHARACTER input — `SELECT` deliberately
 *  excluded. Only for a caller whose documented behaviour is that a focused
 *  dropdown is not typing; everywhere else use {@link isEditableElement}. */
export function isTypingElement(node: EventTarget | null | undefined): boolean {
  const el = asElement(node)
  if (!el) return false
  const tag = el.tagName
  if (tag === 'INPUT' || tag === 'TEXTAREA') return true
  return el.isContentEditable === true
}

/** The minimum an event has to offer to be classified. Widened from `Event` so
 *  a caller can classify a synthetic event object. */
export interface EditableTargetEvent {
  target: EventTarget | null
  composedPath?: () => EventTarget[]
}

/**
 * The editable element a keyboard event came from, or `null` when the keystroke
 * is free for a global hotkey to claim.
 *
 * Reads the event's COMPOSED PATH rather than `event.target`. The code editor's
 * editable node is a `contentEditable` element inside an OPEN shadow root, and a
 * composed event crossing that boundary is retargeted: by the time a
 * document-level listener runs, `event.target` reports the shadow HOST, which
 * answers `false` to every editability question. So a guard reading it lets the
 * keystroke through and the caret loses the character.
 *
 * `composedPath()[0]` is the node that actually holds the caret; the entries
 * after it are its ancestors across every shadow boundary, which is what makes
 * an INHERITED `contentEditable` visible here too. `event.target` is the
 * fallback for an event object that carries no path.
 *
 * Note for tests: jsdom does not retarget composed shadow events, so a
 * document-level listener there still sees the inner node and a `target`-only
 * guard appears to work. Only a real engine shows the bug.
 */
export function editableEventTarget(e: EditableTargetEvent): HTMLElement | null {
  const path = typeof e.composedPath === 'function' ? e.composedPath() : []
  if (path.length === 0) return isEditableElement(e.target) ? asElement(e.target) : null
  for (const node of path) {
    if (isEditableElement(node)) return asElement(node)
  }
  return null
}

/** True when a keyboard event originates inside an editable field. See
 *  {@link editableEventTarget} for why this reads the composed path. */
export function isEditableTarget(e: EditableTargetEvent): boolean {
  return editableEventTarget(e) !== null
}

/**
 * The element that really holds focus, descending through every open shadow
 * root.
 *
 * `document.activeElement` stops at the outermost boundary: with the caret in
 * the code editor it reports the editor's shadow HOST, not the editable node
 * inside — the same blindness `editableEventTarget` fixes for events. Each
 * shadow root keeps its own `activeElement`, so the real one is found by
 * following that chain down.
 */
export function deepActiveElement(): HTMLElement | null {
  let active = asElement(document.activeElement)
  while (active?.shadowRoot?.activeElement) {
    const inner = asElement(active.shadowRoot.activeElement)
    if (!inner || inner === active) break
    active = inner
  }
  return active
}

/** True when focus currently sits in an editable field, shadow roots included.
 *  The counterpart to {@link isEditableTarget} for code that has no event. */
export function activeElementIsEditable(): boolean {
  return isEditableElement(deepActiveElement())
}
