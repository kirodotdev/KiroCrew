/** The `dashboard_capture` frame: a Bot asking to read its own live page.
 *
 *  The gateway sends it to every owner socket with `{ request_id, slug }`. It is
 *  handed to whichever mounted dashboard shows that crewmate, which answers only
 *  while this tab is visible (`useDashboardLiveCapture`). A frame for a crewmate
 *  nothing here shows is dropped, so this tab sends nothing.
 *
 *  Keyed by SLUG for the reason `dashboardFields` is: two tabs can show two
 *  crewmates, and a request belongs to exactly one of them.
 */

export const DASHBOARD_CAPTURE_FRAME = 'dashboard_capture'

type Listener = (requestId: string) => void

const listeners = new Map<string, Set<Listener>>()

/** Listen for *slug*'s capture requests. Returns its own disposer. */
export function subscribeDashboardCapture(slug: string, fn: Listener): () => void {
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
    if (held.size === 0) listeners.delete(slug)
  }
}

/** Route one `dashboard_capture` frame, read defensively like every frame handler. */
export function handleDashboardCapture(data: unknown): void {
  const frame = (data ?? {}) as { slug?: unknown; request_id?: unknown }
  const slug = typeof frame.slug === 'string' ? frame.slug : ''
  const requestId = typeof frame.request_id === 'string' ? frame.request_id : ''
  if (!slug || !requestId || requestId.length > 64) return
  const held = listeners.get(slug)
  if (!held) return
  for (const fn of Array.from(held)) {
    try {
      fn(requestId)
    } catch {
      // One dashboard's throw must not cost another's answer.
    }
  }
}
