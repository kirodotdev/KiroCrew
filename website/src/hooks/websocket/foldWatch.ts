/** Per-tab fold watch: tell the gateway which work boards this tab shows.
 *
 *  The socket declares `fold_delta.1` in `?caps=` (see `connection.ts`). A
 *  gateway that routes slot-fold frames by watch set answers with one
 *  `fold_hello` frame; only then does this tab send `fold_watch`, and again
 *  whenever the set of shown boards changes. A gateway that predates the
 *  capability sends no hello, so the tab sends nothing and keeps receiving
 *  every board's frames, as before. */
import type { QueryClient } from '@tanstack/react-query'

/** The frame a capable gateway sends right after connect. */
export const FOLD_HELLO_FRAME = 'fold_hello'

/** The roots of every mounted `['command-center', root, 'work']` query: the
 *  boards this tab shows. Sorted, so two equal sets compare equal. */
export function watchedBoards(queryClient: QueryClient): string[] {
  const roots = new Set<string>()
  for (const query of queryClient.getQueryCache().getAll()) {
    const key = query.queryKey
    if (key.length !== 3 || key[0] !== 'command-center' || key[2] !== 'work') continue
    if (typeof key[1] !== 'string' || !key[1]) continue
    if (query.getObserversCount() === 0) continue
    roots.add(key[1])
  }
  return [...roots].sort()
}

export interface FoldWatch {
  /** A new socket opened: no hello yet, nothing sent on it yet. */
  reset(): void
  /** The gateway said hello: send the current set. */
  onHello(): void
  /** Stop following the query cache. */
  dispose(): void
}

/** Follow *queryClient*'s mounted work boards and send `fold_watch` through
 *  *send* once the gateway said hello, and on each change after that. */
export function createFoldWatch(queryClient: QueryClient, send: (payload: object) => void): FoldWatch {
  let helloed = false
  let lastSent: string | null = null

  const sync = () => {
    if (!helloed) return
    const slots = watchedBoards(queryClient)
    const signature = JSON.stringify(slots)
    if (signature === lastSent) return
    lastSent = signature
    send({ type: 'fold_watch', slots })
  }

  const unsubscribe = queryClient.getQueryCache().subscribe((event) => {
    if (!helloed) return
    if (event.type === 'observerAdded' || event.type === 'observerRemoved' || event.type === 'removed') sync()
  })

  return {
    reset() {
      helloed = false
      lastSent = null
    },
    onHello() {
      helloed = true
      lastSent = null
      sync()
    },
    dispose() {
      unsubscribe()
    },
  }
}
