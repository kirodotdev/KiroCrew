import { useEffect } from 'react'
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
 * Ask the gateway for Mate's first greeting once its pinned thread is open.
 *
 * `enabled` is true only on Mate's thread (with the Crewmates preview on), and
 * `slotKey` only once the thread endpoint has confirmed it, so the request
 * never races the thread's creation. The server decides: it starts a real turn
 * only for the Mate row the first-crewmate step created while it still owes
 * its welcome, and only while that thread is empty; every other answer is a
 * no-op here.
 *
 * A failure says nothing: the greeting is a courtesy, the chat below works
 * without it, and the person can simply write. The request is forgotten, so the
 * next open of the thread asks again.
 */
export function useFirstGreeting(slug: string | undefined, slotKey: string, enabled: boolean): void {
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
        if (res?.outcome === 'started') store.dispatch(startServerTurn({ slot: slotKey, epoch }))
      },
    })
  }, [enabled, slug, slotKey, mutate])
}
