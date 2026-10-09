import { useCallback, useEffect, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../../api/client'
import { store } from '../../store'
import { selectSlotRunEpoch, startServerTurn } from '../../store/chatSlice'

/** Thread slot keys this tab has already asked to greet. Module-level so a
 *  return to a crewmate (another member in between, a remount) does not ask
 *  again; the server's own once-only marker is what actually guarantees one
 *  greeting, across tabs and reloads. A request that FAILED is taken back out,
 *  so a later open (or the Retry the page offers) can ask again. */
const requested = new Set<string>()

/** Test seam: forget what this tab asked, so each test starts clean. */
export function resetFirstGreetingRequests(): void {
  requested.clear()
}

export interface FirstGreeting {
  /** The greeting request for the current thread failed (a transport error or
   *  a non-2xx answer). The chat stays usable; the page says so and offers
   *  `retry`. */
  failed: boolean
  /** A retry is in flight. */
  retrying: boolean
  /** Ask again for the current thread. */
  retry: () => void
}

/**
 * Ask the gateway for Mate's first greeting once its pinned thread is open.
 *
 * `enabled` is true only on Mate's thread (with the Crewmates preview on), and
 * `slotKey` only once the thread endpoint has confirmed it, so the request
 * never races the thread's creation. The server decides: it starts a real turn
 * only for the Mate row the first-crewmate step created while it still owes
 * its welcome, and only while that thread is empty; every other answer is a
 * no-op here. A failed request is reported through `failed` (the page renders
 * it) and forgotten, so it can be asked again.
 */
export function useFirstGreeting(slug: string | undefined, slotKey: string, enabled: boolean): FirstGreeting {
  const { mutate, isPending } = useMutation({
    mutationFn: ({ slug: s }: { slug: string; slot: string }) => api.memberGreet(s),
    // Hook-level, so it runs even when the page unmounted before the answer
    // (a per-call onError is skipped then): a failed request must leave the
    // tab-wide set, or a later open of the thread would never ask again.
    onError: (_err, { slot }) => {
      requested.delete(slot)
    },
  })
  // The thread whose last request failed; a failure belongs to that thread only.
  const [failedSlot, setFailedSlot] = useState('')

  const ask = useCallback((s: string, slot: string) => {
    requested.add(slot)
    // The greeting turn writes no row until the model's first output, so the
    // thread reads idle for those seconds and a message sent then would draw
    // an optimistic bubble AND the server's queued copy. `started` marks the
    // turn busy; the epoch taken now lets a turn that already sent frames
    // (or already ended) keep the state those frames gave it.
    const epoch = selectSlotRunEpoch(store.getState(), slot)
    setFailedSlot((cur) => (cur === slot ? '' : cur))
    mutate({ slug: s, slot }, {
      onSuccess: (res) => {
        if (res?.outcome === 'started') store.dispatch(startServerTurn({ slot, epoch }))
      },
      onError: () => {
        setFailedSlot(slot)
      },
    })
  }, [mutate])

  useEffect(() => {
    if (!enabled || !slug || !slotKey || requested.has(slotKey)) return
    // A failure already shown for this thread waits for the person's Retry
    // rather than re-asking on every render of the page.
    if (failedSlot === slotKey) return
    ask(slug, slotKey)
  }, [enabled, slug, slotKey, ask, failedSlot])

  const retry = useCallback(() => {
    if (!enabled || !slug || !slotKey || requested.has(slotKey)) return
    ask(slug, slotKey)
  }, [enabled, slug, slotKey, ask])

  return { failed: enabled && !!slotKey && failedSlot === slotKey, retrying: isPending, retry }
}
