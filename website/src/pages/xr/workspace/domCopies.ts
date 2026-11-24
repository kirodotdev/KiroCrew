/**
 * Which page the tab-drawn path copies into the room in this frame (`domSource.ts`). A copy is the whole page, the
 * room's dearest work, so the room copies AT MOST ONE page a frame (`BACKLOG.md`, the ruling of 19 Nov 21:00, B1).
 * Among the pages in view that the browser painted anew:
 *   1. a page that waited `MOST_WAIT` frames or more, the longest wait first: no page starves;
 *   2. else the page that took his input (a key, a scroll) since its last copy: it shows in the next frame;
 *   3. else the page under the pointer, unless it had the last frame and another waits;
 *   4. else the page nearest the middle of his look, under the same condition;
 *   5. else the page that waited longest.
 * A page that changes alone gets every frame; a still page costs nothing. Pure: `DomSource` keeps the waits.
 */

/** A page that waits for a copy: how many frames it waited, whether the pointer is on it, how far it is from his look. */
export interface CopyWant { id: string; waited: number; pointer: boolean; gazeDeg: number; input?: boolean }

/** A page that waited this many room frames is copied before any other. */
export const MOST_WAIT = 6

/** The page to copy in this frame, or `null` when none waits. `last`: the page that had the last frame. */
export function pickCopy(wants: readonly CopyWant[], last: string | null): string | null {
  if (wants.length === 0) return null
  const longest = (list: readonly CopyWant[]) => list.reduce((a, b) => (b.waited > a.waited ? b : a))
  const starved = wants.filter(w => w.waited >= MOST_WAIT)
  if (starved.length) return longest(starved).id
  // A page that had the last frame waits its turn while another waits.
  const fair = (w: CopyWant | undefined) => !!w && !(w.id === last && wants.length > 1)
  const typed = wants.find(w => w.input)
  if (typed) return typed.id
  const pointed = wants.find(w => w.pointer)
  if (fair(pointed)) return (pointed as CopyWant).id
  const looked = wants.reduce((a, b) => (b.gazeDeg < a.gazeDeg ? b : a))
  if (fair(looked)) return looked.id
  return longest(wants.filter(w => w.id !== last).length ? wants.filter(w => w.id !== last) : wants).id
}
