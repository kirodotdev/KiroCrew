/** When the session lane may start animating its rows. */
import { useEffect, useState } from 'react'

/**
 * False until the lane's first population has painted, then true for the life
 * of the mount.
 *
 * WHY: a row's Framer layout projection animates any position change between two
 * commits, and the first commits of a fresh lane are full of changes that are not
 * movement. Every `.session-row` is `content-visibility: auto` with a 60px
 * intrinsic size, so the first layout places rows at that placeholder height and
 * the next one at their real height; the folder, tag-column and slot reads then
 * land one by one and refile rows. Springing each of those commits, and playing
 * every row's mount slide-in, makes the list visibly collapse and re-expand as it
 * loads. The slower the reads (a remote crew's pane, over its tunnel), the further
 * apart those commits land and the more of it the user sees.
 *
 * While this is false, rows snap to each new layout and skip the slide-in. They
 * keep their projection props all the same: Framer copies `layout` and
 * `layoutId` into the projection node once, when the element mounts, so a row
 * mounted without them would never glide on a later reorder.
 *
 * Held until `readsSettled` (the reads that decide where rows sit) and then two
 * animation frames, so the rows have painted at their real size before the first
 * snapshot is taken. There is no time cap: a read that is merely slow must not
 * hand the list back to animation halfway through, and every input the caller
 * passes settles on its own (a query on success or its last retry, the slot list
 * once one has been applied). Rows that mount AFTER that keep their slide-in,
 * which is the point of it: a session that appears while the list is on screen.
 * The sidebar root mirrors the answer as `data-row-anim` (`holding` / `settled`).
 */
export function useRowAnimSettled(readsSettled: boolean): boolean {
  const [settled, setSettled] = useState(false)
  useEffect(() => {
    if (settled || !readsSettled) return
    let inner = 0
    const outer = requestAnimationFrame(() => { inner = requestAnimationFrame(() => setSettled(true)) })
    return () => {
      cancelAnimationFrame(outer)
      cancelAnimationFrame(inner)
    }
  }, [readsSettled, settled])
  return settled
}
