/**
 * The layout scope value — the pure-data subject a cell reads to know what to
 * render (RFC §2.2, §5.3).
 *
 * PR 1 ships ONLY the DATA type here: the `Subject` shape and the fixed key it
 * is read under. The runtime scope bus (a `Map<key, Subject>` with a parent
 * pointer, its React context and `useSyncExternalStore` reader) is the render
 * path and lands with the renderer (PR 3) — it is not model code. The subject
 * is runtime-only and is NEVER serialized into a stored layout (RFC §5.1).
 */

/**
 * The Phase-1 scope value: the degenerate stand-in for a typed entity ref.
 * On the Members page this is the selected crewmate's session slot.
 */
export interface Subject {
  /** The slot/session key — what ChatPane's `slotKey` and SidePanel's `slot`
   *  already read. This is the ENTIRE input contract for a cell: identical to
   *  the argument the tabbed panel (`usePanelTabs(slot)`) and `ActivityViewer`
   *  take today. It is deliberately just a slot, not a member/entity id — a
   *  future typed-entity layer would WIDEN this interface to reflect what an
   *  element needs to function, rather than coupling every element to a session. */
  slot: string
}

/** The single degenerate key every Phase-1 cell publishes or reads. */
export const SUBJECT_KEY = 'subject'
