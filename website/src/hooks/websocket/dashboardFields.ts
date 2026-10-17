/** The field frames a crewmate's dashboard page is refilled from.
 *
 *  The gateway pushes only the fields whose value MOVED, each frame carrying a
 *  version that steps by one. A query invalidation cannot deliver that: the tab's
 *  other two liveness frames mean "re-read the whole page", and re-reading would
 *  throw away the page's own state -- a chart's animation, a tree's scroll
 *  position, a 3D scene -- on every value change, which is the thing the push
 *  exists to avoid.
 *
 *  So a frame is handed to whichever mounted dashboard is showing that crewmate,
 *  and that component posts it into its iframe. Keyed by SLUG because two tabs can
 *  show two crewmates at once and a frame belongs to exactly one of them.
 *
 *  A listener registry rather than redux: the payload is not view state, nothing
 *  else reads it, and a reducer would make a value that is already frozen inside
 *  the frame a second time in a store nobody selects from.
 */

export const DASHBOARD_FIELDS_FRAME = 'dashboard_fields'

/** One frame's read: the payload shape `dashboard_frame.read_payload` builds. */
export type FieldRead = { [key: string]: unknown }

/** `dashboard` is the package the frame belongs to; a listener drops any other. */
type Listener = (read: FieldRead, dashboard: string) => void

const listeners = new Map<string, Set<Listener>>()

/** Listen for *slug*'s field frames. Returns its own disposer. */
export function subscribeDashboardFields(slug: string, fn: Listener): () => void {
  if (!slug) return () => {}
  let set = listeners.get(slug)
  if (!set) {
    set = new Set()
    listeners.set(slug, set)
  }
  set.add(fn)
  return () => {
    const held = listeners.get(slug)
    if (!held) return
    held.delete(fn)
    // The LAST listener takes the key with it, so a tab opened and closed a hundred
    // times leaves no entry behind. The map is keyed by a slug the server chose, so
    // an entry that is never cleaned is a leak for the life of the page.
    if (held.size === 0) listeners.delete(slug)
  }
}

/** Route one `dashboard_fields` frame to the dashboard showing that crewmate.
 *
 *  Read DEFENSIVELY, like every other frame handler: a socket carries whatever the
 *  gateway sent, and a malformed frame must cost this refill rather than the
 *  dispatch of the next one. A frame for a crewmate nothing is showing is dropped,
 *  which is the ordinary case -- the socket is per connection, not per tab.
 */
export function handleDashboardFields(data: unknown): void {
  const frame = (data ?? {}) as { slug?: unknown; dashboard?: unknown; read?: unknown }
  const slug = typeof frame.slug === 'string' ? frame.slug : ''
  const dashboard = typeof frame.dashboard === 'string' ? frame.dashboard : ''
  const read = frame.read
  if (!slug || !read || typeof read !== 'object') return
  const held = listeners.get(slug)
  if (!held) return
  // A COPY of the set, because a listener may unsubscribe during delivery and
  // mutating the set under the iteration would skip its neighbour.
  for (const fn of Array.from(held)) {
    try {
      fn(read as FieldRead, dashboard)
    } catch {
      // One dashboard's throw must not cost another's refill. There is at most one
      // per slug today, which is exactly why this is cheap to get right now.
    }
  }
}
