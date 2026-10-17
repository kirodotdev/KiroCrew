import { useState, useRef, useCallback, useEffect } from 'react'
import { useMutation } from '@tanstack/react-query'
import { api } from '../api/client'

/** How long the spinner may outlive a press that neither starts a turn nor is refused. */
const SPINNER_BACKSTOP_MS = 30_000

/**
 * The Resume press and its spinner, owned once for every surface that offers
 * it: ChatPage (`useTurnRecovery`, the active slot) and `ChatPane`
 * (`useChatPaneContinue`, the slot a pane shows), so the two cannot drift.
 *
 * The press posts `continue` for `slot`. Nothing is written to the transcript
 * here: the backend appends the continuation as an `inject` row and the slots
 * frame flips `running`, so the request settling is not the success signal and
 * `continuing` outlives it. It ends on whichever comes first: the turn running
 * (`turnRunning`), the gateway's refusal (handed to `onRefused`), a different
 * slot, or the backstop for a request that does neither.
 *
 * A press is about ONE slot. The Members page and the session tabs re-point a
 * mounted surface at another session, so a refusal that comes back for the slot
 * it used to show is dropped, not shown against the new one. What the refusal
 * shows, and when an old one is retired, belong to the surface that owns the
 * notice, which is why `onRefused` is a callback.
 */
export function useContinuePress({ slot, turnRunning, continuable, onRefused }: {
  slot: string | null
  /** A turn is running on the slot: the press took. */
  turnRunning: boolean
  /** The press is safe now (nothing running, stopping or queued); see `selectContinuable`. */
  continuable: boolean
  /** The gateway refused the press (a turn already running, a stop in progress, a pending approval). */
  onRefused: (error: unknown) => void
}) {
  const [continuing, setContinuing] = useState(false)
  const pressSlotRef = useRef(slot)
  useEffect(() => {
    pressSlotRef.current = slot
    setContinuing(false)
  }, [slot])
  // The turn taking over is the success signal; clear the spinner then.
  useEffect(() => { if (continuing && turnRunning) setContinuing(false) }, [continuing, turnRunning])
  useEffect(() => {
    if (!continuing) return
    const t = setTimeout(() => { setContinuing(false) }, SPINNER_BACKSTOP_MS)
    return () => clearTimeout(t)
  }, [continuing])
  const { mutate: sendContinue } = useMutation({
    mutationFn: (key: string) => api.continueSlot(key),
    onError: (e: unknown, key: string) => {
      if (pressSlotRef.current !== key) return
      onRefused(e)
      setContinuing(false)
    },
  })
  const press = useCallback(() => {
    if (!slot || continuing || !continuable) return
    setContinuing(true)
    sendContinue(slot)
  }, [slot, continuing, continuable, sendContinue])
  return { continuing, press }
}
