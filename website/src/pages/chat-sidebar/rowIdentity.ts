/** Row identity and the origin test on local metadata. A peer row can carry a slot
 *  key byte-identical to a local one, so a key-indexed lookup of pin, folder or unread
 *  state must reject the peer origin first: the row model (`./rows`) does it once for
 *  every row it builds, and the owners that hold their own lists use these helpers. */
import type { Slot } from './types'

/** Does this row belong to ANOTHER machine? The one question every local-only
 * affordance in the sidebar asks, behind one name so the answer cannot drift
 * between call sites.
 *
 * Reads `peer_id`, never `executor`/`instance_id`: a slot with
 * `executor: 'remote'` is a LOCAL session that merely runs its turns on a peer,
 * so it keeps every affordance below and must answer `false` here. See the
 * `peer_id` doc on `Slot`. */
export function isPeerRow(slot: Pick<Slot, 'peer_id'>): boolean {
  return !!slot.peer_id
}

/** Remote and local gateways do not share a slot-key namespace. Deterministic
 * member/channel keys can be byte-identical, so every UI identity includes the
 * origin while local rows preserve their existing key. */
export function sessionRowIdentity(slot: Pick<Slot, 'key' | 'peer_id' | 'row_identity'>): string {
  // The SERVER's answer wins when it has one. `row_identity` is projected on every
  // local slot and resolves a remote-bound session — minted on a crew or adopted
  // from a peer row — to `<instance_id>:<peer_key>`, which is the identity the peer
  // row already had. Same identity before and after the bind means this row is
  // re-rendered rather than replaced: the row the user clicked becomes the session
  // they asked for, instead of a sibling appearing next to it. It also keeps a
  // `data-session-row` selector and everything logged about the row continuous
  // across the adopt.
  //
  // The fallback covers only an older payload with no `row_identity` at all. A
  // peer row does NOT need one: `/chat-slots` stamps the server-resolved identity
  // on every shaped row, so this reader no longer composes the format itself.
  // Composing it here made `<instance_id>:<peer_key>` a contract in two places
  // whose equality nothing checked, and that equality is what keeps an adopted
  // row one row instead of two.
  if (slot.row_identity) return slot.row_identity
  return slot.key
}

/** The Older Sessions pane's counterpart to `sessionRowIdentity`, for the same
 * reason and against a DIFFERENT field.
 *
 * A federated history row names the peer that answered the search in
 * `instance_id` — that pane is the one place where `instance_id` already means
 * ownership rather than execution, because a history row has no turns to
 * dispatch. The LIVE list uses `peer_id` instead (see the `Slot` docs), so the
 * two panes cannot share one reader: whichever one it keyed on, the other pane's
 * rows would fall back to their raw key, and a local/remote pair whose
 * deterministic keys collide would then reconcile as ONE React child and lose a
 * row. Two collections carrying two origin fields get two readers, and the
 * narrower parameter type is what stops either from being called on the other's
 * rows by accident. */
export function historyRowIdentity(item: { key: string; instance_id?: string }): string {
  return item.instance_id ? `${item.instance_id}:${item.key}` : item.key
}

/** Local sidebar metadata is keyed only by local slot key. A remote peer may
 * emit the same deterministic key, so mixed collections must reject the remote
 * origin before consulting folder state. The row model (`./rows`) answers this for
 * the rows it builds; this reader serves the owners that hold their own lists. */
export function localSlotFolder(
  slot: Pick<Slot, 'key' | 'peer_id'>,
  slotFolders: Readonly<Record<string, string>>,
): string | undefined {
  return isPeerRow(slot) ? undefined : slotFolders[slot.key]
}
