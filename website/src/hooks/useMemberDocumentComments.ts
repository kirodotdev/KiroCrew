import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'
import { sendTurn } from '../chat-core/transport/sendTurn'
import { commentsRefusedCopy, commentsSentCopy, deliveryUnconfirmedCommentsCopy } from '../chat-core/transport/receiptCopy'
import { slotBusySteer } from '../components/chat-input/busySend'
import { useAppStore } from '../store'
import { useJevAutoSend } from '../pages/chat/useJevAutoSend'

/** How long the "N comments sent" status line stays up. */
const SENT_LINE_MS = 5000

interface Options {
  /** Gateway connection flag: nothing is sent while it is down. */
  connected: boolean
  /** The confirmed DM slot, read at the press. The document actions stamp every
   *  tab with it, on a strip bucketed per crewmate, so a batch only ever reaches
   *  the crewmate the panel belongs to. */
  slotRef: MutableRefObject<string | null>
  /** True while the side panel sits beside the thread; false in overlay placement,
   *  where a failure closes the panel (see `showActionError`). */
  besideRef: MutableRefObject<boolean>
  /** The crewmate's display name, for the refused notice. */
  crewmateLabel: string | undefined
  /** Puts a failure notice above the thread. In overlay placement it also closes
   *  the overlay, which would otherwise cover the notice. */
  showActionError: (message: string) => void
  /** Clears an earlier failure notice once a send is delivered. */
  clearActionError: () => void
}

/**
 * Inline comments on a document in the crewmate page's side panel. The panel's
 * Submit sends the batch it formatted into this crewmate's chat the way the chat
 * page sends it into the session the file was opened from: the chat-core
 * transport, with the composer's busy-send decision for that slot (steer, queue
 * or auto). While the thread is unconfirmed (a revalidation, a refusal) the
 * panel withholds its document tabs with the other slot-bound views, so Submit
 * is not on screen; the slot check below is the backstop and sends nothing.
 *
 * The outcome shows outside the DM transcript -- the crewmate's DM stays
 * messages-only per the 2026-10-09 recorded decision:
 *  - refused: `showActionError` with `commentsRefusedCopy`; the server's reason
 *    is logged, never shown. The comments stay pending.
 *  - transport-error / response-late: `showActionError` with
 *    `deliveryUnconfirmedCommentsCopy`. Delivery is indeterminate for both, so
 *    the comments stay pending and the copy says to send them again if they are
 *    not in the conversation within a minute. (The chat page counts a late,
 *    non-steer send as delivered because it keeps an optimistic row in the
 *    thread; this page draws none, so it keeps the batch.)
 *  - dispatched / queued / unknown: "N comments sent" in `info`. `unknown` is a
 *    2xx whose body could not be read: the server took the request, so the batch
 *    counts as delivered, as the chat page's send verdict counts it.
 * Both failure copies take the placement the notice is shown in: in overlay
 * placement the panel has just closed, so they also say to reopen it. Every
 * delivered outcome also clears an earlier failure notice.
 *
 * Returns the panel's `onSubmitComments` (its boolean verdict tells the panel
 * whether to clear the batch) and the status line to render.
 */
export function useMemberDocumentComments({
  connected, slotRef, besideRef, crewmateLabel, showActionError, clearActionError,
}: Options) {
  const appStore = useAppStore()
  const jevAutoConsented = useJevAutoSend()
  const [info, setInfo] = useState('')
  const infoTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (infoTimerRef.current) clearTimeout(infoTimerRef.current) }, [])

  const submitComments = useCallback(async (message: string, count = 0) => {
    if (!connected) return false
    const slot = slotRef.current
    if (!slot) return false
    const steer = slotBusySteer(appStore.getState(), slot, jevAutoConsented)
    const receipt = await sendTurn({ message, slot, ...(steer ? { steer } : {}) })
    if (receipt.status === 'refused') {
      // Log the raw server reason for debugging; never show it in the UI.
      // eslint-disable-next-line no-console -- server refusal reason for debugging; the banner carries the user-friendly sentence
      console.warn('[members] submitComments refused:', receipt.reason)
      showActionError(commentsRefusedCopy(crewmateLabel, !besideRef.current))
      return false
    }
    if (receipt.status === 'transport-error' || receipt.status === 'response-late') {
      showActionError(deliveryUnconfirmedCommentsCopy(!besideRef.current))
      return false
    }
    // dispatched, queued or unknown: the batch is delivered.
    clearActionError()
    if (count > 0) {
      if (infoTimerRef.current) clearTimeout(infoTimerRef.current)
      setInfo(commentsSentCopy(count))
      infoTimerRef.current = setTimeout(() => setInfo(''), SENT_LINE_MS)
    }
    return true
  }, [connected, slotRef, besideRef, appStore, jevAutoConsented, crewmateLabel, showActionError, clearActionError])

  return { info, submitComments }
}
