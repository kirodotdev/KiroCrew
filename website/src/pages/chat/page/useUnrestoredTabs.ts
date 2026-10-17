import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from '../../../api/client'
import { useAppSelector } from '../../../store'

/** Where a dismissal is remembered, keyed on the count so a LATER, larger loss
 *  still speaks up instead of inheriting the earlier dismissal. */
const DISMISSED_KEY = 'kc.unrestoredTabs.dismissed'

/** How often to re-ask while the restore has not answered. */
const UNREPORTED_POLL_MS = 5000

function dismissedCount(): string | null {
  try { return sessionStorage.getItem(DISMISSED_KEY) }
  catch { return null }
}

/**
 * How many tabs this gateway's startup restore listed but could not show.
 *
 * Read through `useQuery` rather than a hand-rolled `useEffect` fetch, so the
 * request inherits the shared retry ladder. The dashboard is commonly served
 * through a fronting proxy that answers a burst with HTTP 429; a manual fetch
 * swallows that and the notice is simply absent, which is the one outcome a
 * notice about silent data loss must not have.
 *
 * It also keeps asking until the restore ANSWERS. The restore runs after the
 * gateway starts serving, so a browser that arrives early reads `reported: false`
 * -- a real state, distinct from "nothing was dropped", and one that resolves on
 * its own within seconds. Polling stops the moment an answer arrives. A gateway
 * restart is the other way the answer changes: the new process runs its own
 * restore, so a WebSocket reconnect re-asks once.
 *
 * `0` covers both "nothing was dropped" and "no answer yet". They are different
 * facts on the wire (`reported`) and deliberately collapse here: the only consumer
 * is a notice, and a notice has nothing to say in either case.
 *
 * Dismissal lives in `sessionStorage`: a reload inside the same browser tab must
 * not re-raise a notice the user already answered, while a genuinely new browser
 * session is a new arrival and asks again.
 */
export function useUnrestoredTabs(): {
  count: number
  dismiss: () => void
  failed: boolean
  unknowable: boolean
} {
  const [dismissed, setDismissed] = useState<string | null>(() => dismissedCount())
  const query = useQuery({
    queryKey: ['chat', 'slots', 'unrestored'],
    queryFn: () => api.chatSlotsUnrestored(),
    // The answer is settled for the life of the gateway process once it arrives, so
    // there is nothing to go stale; the two ways it changes are handled explicitly.
    staleTime: Infinity,
    refetchInterval: ({ state }) => (state.data?.reported ? false : UNREPORTED_POLL_MS),
  })

  // A gateway restart drops the socket and brings up a process with its own restore
  // result, so the false -> true edge is the signal. The initial `true` is not an
  // edge: the query above has already asked.
  const connected = useAppSelector(s => s.dashboard.connected)
  const wasDisconnected = useRef(false)
  const refetch = query.refetch
  useEffect(() => {
    if (!connected) {
      wasDisconnected.current = true
      return
    }
    if (!wasDisconnected.current) return
    wasDisconnected.current = false
    void refetch()
  }, [connected, refetch])

  const reported = query.data?.reported === true
  const raw = reported && typeof query.data?.count === 'number' ? query.data.count : 0
  const count = raw > 0 && dismissed !== String(raw) ? raw : 0

  const dismiss = useCallback(() => {
    try { sessionStorage.setItem(DISMISSED_KEY, String(raw)) } catch { /* private mode */ }
    setDismissed(String(raw))
  }, [raw])

  // Reported, not swallowed. The read exists to tell the user about silent data
  // loss, so a read that FAILED is the one state where saying nothing repeats the
  // defect this whole change is about: the user is left believing nothing was
  // dropped because the question could not be asked. It clears on its own once a
  // refetch succeeds, so there is nothing to dismiss.
  //
  // `unknowable` is the same silence reached from the other side: the question WAS
  // asked and answered, and the answer is that the gateway could not read which
  // tabs were open. `count` is then not a measurement, so it is not returned as
  // one -- the caller renders the same "could not check" notice it renders for a
  // failed read, and the poll stops because this answer is settled.
  return { count, dismiss, failed: query.isError, unknowable: reported && query.data?.unknowable === true }
}
