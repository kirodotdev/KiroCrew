/**
 * Which mates the left dock shows, and in what order.
 *
 * A starred mate is a dock row: the star (`starred` on the crew record) is the
 * only switch, so there is no second "pin" to keep in step with it. The order
 * is the record's `star_order`, written when the person drags a dock row and
 * stored on the server beside the star, so it follows them to every device.
 *
 * `star_order` 0 means "not placed yet": a mate starred after the last drag.
 * Those sort after every placed mate, in roster order, which is what puts a
 * newly starred mate at the bottom. The server clears the field on unstar, so
 * a re-star lands at the bottom too instead of in its old slot.
 */
import type { MemberRosterRow } from '../api/client'

/**
 * How many starred mates the dock shows while its Mates group is folded.
 *
 * Every row here takes height from the installed apps below it, which share
 * the same leftover room in the rail. Three keeps both groups readable on an
 * ordinary window. The group folds behind the Apps list's own "N more" /
 * "Show less" toggle: unfolded, it lists every starred mate and any of them
 * can be dragged to any position, which is how the drag order decides which
 * three show folded. The phone tiles show these three with no toggle.
 */
export const DOCK_MATES_LIMIT = 3

/** The starred rows of `roster`, in dock order. */
export function dockMateOrder(roster: readonly MemberRosterRow[]): MemberRosterRow[] {
  return roster
    .map((m, index) => ({ m, index }))
    .filter(({ m }) => m.starred)
    .sort((a, b) => {
      const ao = a.m.star_order ?? 0
      const bo = b.m.star_order ?? 0
      if ((ao === 0) !== (bo === 0)) return ao === 0 ? 1 : -1
      return ao - bo || a.index - b.index
    })
    .map(({ m }) => m)
}

/**
 * The `star_order` writes a drag needs: move the row at `from` to `to` in
 * `ordered` (the full dock order, not only the rows on screen), number every
 * row 1..n, and return a write for every row. A cached `star_order` can be
 * stale (another device, an unstar the cache missed), so skipping rows whose
 * cached value already matches could leave the server with a different order.
 */
export function starOrderWrites(
  ordered: readonly MemberRosterRow[],
  from: number,
  to: number,
): { name: string; star_order: number }[] {
  if (from === to || from < 0 || to < 0 || from >= ordered.length || to >= ordered.length) return []
  const next = [...ordered]
  const [moved] = next.splice(from, 1)
  next.splice(to, 0, moved)
  return next.map((m, i) => ({ name: m.name, star_order: i + 1 }))
}
