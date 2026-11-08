import { useEffect, useRef } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import { store } from '../../store'
import { selectSlotRunEpoch, startServerTurn } from '../../store/chatSlice'

/** Thread slot keys this tab has already asked to greet. Module-level so a
 *  return to a crewmate (another member in between, a remount) does not ask
 *  again; the server's own once-only marker is what actually guarantees one
 *  greeting, across tabs and reloads. A request that FAILED is taken back out,
 *  so a later open of the thread asks again. */
const requested = new Set<string>()

/** Test seam: forget what this tab asked, so each test starts clean. */
export function resetFirstGreetingRequests(): void {
  requested.clear()
}

/**
 * Ask the gateway for a crewmate's first greeting once its pinned thread is open.
 *
 * `enabled` is true only for a crewmate that can be owed one (Mate, and a
 * crewmate created on the dashboard), and
 * `slotKey` only once the thread endpoint has confirmed it, so the request
 * never races the thread's creation. The server decides: it starts a real turn
 * only for a crewmate whose creation recorded the welcome it owes (Mate's
 * first-crewmate step, or a create that sent `first_greeting`), and only while
 * that thread is empty; every other answer is a no-op here.
 *
 * A failure says nothing: the greeting is a courtesy, the chat below works
 * without it, and the person can simply write. The request is forgotten, so the
 * next open of the thread asks again.
 *
 * `onStarted` fires with the thread's slot key for exactly the answer that
 * means the greeting was dispatched NOW (`started`) and for no other: a thread
 * already greeted, a reload, a second tab or a failed request never fires it.
 * The first Mate's entrance (#23207) keys on it, which is what makes the
 * entrance once-only without a marker of its own: the server's `O_EXCL` claim
 * already decides which single request ever hears `started`.
 */
export function useFirstGreeting(
  slug: string | undefined, slotKey: string, enabled: boolean, onStarted?: (slot: string) => void,
): void {
  // Read at answer time, so a new callback identity never re-arms the effect
  // below (which would ask again) and the latest one is what hears `started`.
  const onStartedRef = useRef(onStarted)
  onStartedRef.current = onStarted
  const { mutate } = useMutation({
    mutationFn: ({ slug: s }: { slug: string; slot: string }) => api.memberGreet(s),
    // Hook-level, so it runs even when the page unmounted before the answer
    // (a per-call onError is skipped then): a failed request must leave the
    // tab-wide set, or a later open of the thread would never ask again.
    onError: (_err, { slot }) => {
      requested.delete(slot)
    },
  })

  useEffect(() => {
    if (!enabled || !slug || !slotKey || requested.has(slotKey)) return
    requested.add(slotKey)
    // The greeting turn writes no row until the model's first output, so the
    // thread reads idle for those seconds and a message sent then would draw
    // an optimistic bubble AND the server's queued copy. `started` marks the
    // turn busy; the epoch taken now lets a turn that already sent frames
    // (or already ended) keep the state those frames gave it.
    const epoch = selectSlotRunEpoch(store.getState(), slotKey)
    mutate({ slug, slot: slotKey }, {
      onSuccess: (res) => {
        if (res?.outcome !== 'started') return
        // The entrance first, then the turn: the store's dispatch renders at
        // once, and the pane must already be veiled when the turn's "Working"
        // row would otherwise show for a frame under the face.
        onStartedRef.current?.(slotKey)
        store.dispatch(startServerTurn({ slot: slotKey, epoch }))
      },
    })
  }, [enabled, slug, slotKey, mutate])
}
