import type { Slot } from './types'

/**
 * The creator a row cites, as the (origin, slot key) pair the lane resolves it in, or
 * null when the row cites nobody it could nest under.
 *
 * `parent.key` is a bare slot key in the key space of the row's OWN gateway, so it is
 * resolved among rows of the same `peer_id`. Every reader that asks "does this creator
 * exist" or "which row is it" goes through this, so no two readers resolve it in
 * different spaces.
 */
export function citedCreatorOf(row: Pick<Slot, 'parent' | 'peer_id'>): { origin: string | undefined; key: string } | null {
  const cited = row.parent?.key
  if (cited == null || cited === '') return null
  return { origin: row.peer_id, key: cited }
}
