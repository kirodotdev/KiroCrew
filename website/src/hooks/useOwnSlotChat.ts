/**
 * Keeps an embedded ChatPage on the session its host hands it.
 *
 * `<ChatPage embedded />` renders the dashboard's ACTIVE slot, whatever it is. A
 * host that embeds it for one session (the artifact companion, the app SDK's
 * ChatPanel) makes that session active and shows the chat only while it is: a
 * switch that fails leaves the chat that was active before on screen, and
 * whatever the user types goes to that other session. The guard is the one
 * Papyrus's co-author panel carries (#22460).
 */
import { startTransition, useEffect, useRef, useState } from 'react'
import { useAppDispatch, useAppSelector } from '../store'
import { switchSlot } from '../store/chatSlice'
import { resolveSlotSuccession } from '../utils/slotSuccession'
import { errMessage, isMissingSlotError } from '../utils/thunkError'

import { i18nT } from '../i18n/t'

// A run of switch-backs, each less than SWITCH_BACK_GAP_MS after the one
// before, stops after MAX_SWITCH_BACKS: see the watch effect.
const MAX_SWITCH_BACKS = 10
const SWITCH_BACK_GAP_MS = 5_000

// The session a memory-mode switch handed `slotKey` over to, if any.
function replacementOf(slotKey: string): string | undefined {
  const live = resolveSlotSuccession(slotKey)
  return live && live !== slotKey ? live : undefined
}

export interface OwnSlotChatOptions {
  /** The host's session, or null when it has none. */
  slotKey: string | null
  /** The gateway answered "not found" for this slot key: the session is gone. */
  onSessionGone: (slotKey: string) => void
  /** The embedded chat replaced the session with a new one (a memory-mode pick). */
  onSessionReplaced?: (oldKey: string, newKey: string) => void
  /** The embedded chat has its own sessions rail: once the session has opened,
   *  a session the user picks there is shown instead of switched away from. */
  followsNavigation?: boolean
}

export interface OwnSlotChat {
  /** Whether the embedded chat may be shown: it would render the host's session. */
  showChat: boolean
  /** Message of a switch failure other than a 404 (gateway down, 5xx). */
  failure: string
  /** Run the switch again, for the failure message's "Try again" button. */
  retry: () => void
}

export function useOwnSlotChat({
  slotKey, onSessionGone, onSessionReplaced, followsNavigation = false,
}: OwnSlotChatOptions): OwnSlotChat {
  const dispatch = useAppDispatch()
  const activeSlot = useAppSelector(state => state.chat.activeSlot)
  const slots = useAppSelector(state => state.dashboard.slots)
  // Where a close of the session (Ctrl+W, the sidebar) stands. `deleteSlot`
  // takes the row out of `slots` before its DELETE answers and keeps a hold in
  // `dashboard.closingSlots`: 'pending' until the gateway answers, 'confirmed'
  // once the gateway has deleted the session. A failed DELETE drops the hold,
  // then the slot list shows the session again.
  const closePhase = useAppSelector(state => {
    const hold = slotKey ? state.dashboard.closingSlots?.[slotKey] : undefined
    if (!hold) return null
    return hold.inFlightUntil === null ? 'confirmed' : 'pending'
  })
  // The last slot list came from a gateway still restoring sessions after a
  // restart, so a session missing from it may still exist.
  const listPartial = useAppSelector(state => state.dashboard.slotsListPartial)
  // Another session's switch is still loading: see the watch effect.
  const switchingElsewhere = useAppSelector(state =>
    state.chat.slotSwitchRequestId !== null && state.chat.slotSwitchTarget !== slotKey)
  // Read when a switch settles; a change to either must not re-run the switch.
  const activeSlotRef = useRef(activeSlot)
  activeSlotRef.current = activeSlot
  const slotsRef = useRef(slots)
  slotsRef.current = slots
  // The session once switchSlot() has made it active. The chat is shown only
  // from then on: during the switch, and after a 404 makes the store put back
  // the chat that was active before, there is no chat at all.
  const [readyKey, setReadyKey] = useState<string | null>(null)
  const [failure, setFailure] = useState('')
  // Bumped to run the switch again: by `retry`, and when the watch effect
  // finds another session active.
  const [attempt, setAttempt] = useState(0)
  // Whether the session has been seen in the slot list since it was shown:
  // see the watch effect.
  const seenInListRef = useRef(false)
  // Whether a close of the session was seen pending since the session was
  // last listed: see the watch effect.
  const closeSeenRef = useRef(false)
  // Set by `retry`: that retry follows a failed switchSlot(), which can leave
  // the session active and listed, so it must not take the shortcut below and
  // show a chat the gateway never loaded.
  const retryRef = useRef(false)
  // The current run of switch-backs to the session: see the watch effect.
  const switchBacksRef = useRef({ key: slotKey, count: 0, at: 0 })

  // Make the session the active slot: when the host hands one over or changes
  // it, and when the watch effect below finds another session active.
  useEffect(() => {
    setFailure('')
    setReadyKey(null)
    // A new session starts a new run of switch-backs; a re-run for the same
    // session (a switch-back, `retry`) keeps the run, so the cap holds.
    if (switchBacksRef.current.key !== slotKey) switchBacksRef.current = { key: slotKey, count: 0, at: 0 }
    if (!slotKey) return
    seenInListRef.current = false
    closeSeenRef.current = false
    const ready = () => setReadyKey(slotKey)
    const retry = retryRef.current
    retryRef.current = false
    // Skip the switch only for a session that is active AND still listed, and
    // never on a retry: a session deleted while the host was closed can still
    // be the active slot, and a failed switch leaves its target active.
    if (!retry && activeSlotRef.current === slotKey && slotsRef.current.some(s => s.key === slotKey)) {
      ready()
      return
    }
    let current = true
    dispatch(switchSlot(slotKey)).unwrap()
      .then(() => { if (current) ready() })
      .catch((err: unknown) => {
        // A panel closed or moved on: the session may have been opened again since.
        if (!current) return
        // A deleted session: the host forgets the key.
        if (isMissingSlotError(err)) onSessionGone(slotKey)
        // Anything else is shown, with a "Try again" button; it is never retried
        // on its own, so a gateway outage does not loop.
        else setFailure(errMessage(err) || i18nT('store.chatSlice.session_open_error'))
      })
    return () => { current = false }
  }, [slotKey, attempt, dispatch, onSessionGone])

  // While the session is shown, watch what the embedded chat does to the
  // active slot. Only a memory-mode switch may move it to another session:
  // that switch records the hand-over (`recordSlotSuccession`), makes the new
  // session active, then deletes the old one, and the host adopts the new one.
  // Any other session becoming active -- a transcript link to another chat, or
  // the neighbour the store falls back to after a delete -- is never shown:
  // a deleted session is reported gone, a live one is made active again. A
  // chat with its own sessions rail is left to it once the session has opened.
  // A control that opens another session and leaves the page (`switchSlot`,
  // awaited or not, then straight to `navigate`) keeps that session. So nothing is
  // switched back while another session's switch is loading, and the switch
  // back is a transition: React renders it together with the route change,
  // which the router also makes a transition, and the host is gone first.
  useEffect(() => {
    if (followsNavigation || !slotKey || readyKey !== slotKey || switchingElsewhere) return
    const switchAgain = () => startTransition(() => setAttempt(n => n + 1))
    const replacement = replacementOf(slotKey)
    const listed = slots.some(s => s.key === slotKey)
    if (listed) {
      seenInListRef.current = true
      closeSeenRef.current = false
    // Not merely absent from a list that has not caught up with a new session.
    } else if (seenInListRef.current) {
      if (closePhase === 'pending') closeSeenRef.current = true
      if (replacement && activeSlot === replacement) {
        // Shown at once, so the chat stays mounted while the host adopts it.
        setReadyKey(replacement)
        onSessionReplaced?.(slotKey, replacement)
      // Reported gone only on the gateway's word: a confirmed close here, or the
      // 404 of the switch below. A close that is pending, or that failed before
      // the slot list shows the session again, is not reported gone.
      } else if (closePhase === 'confirmed') {
        onSessionGone(slotKey)
      // Absent without a close: deleted elsewhere, or left out of a partial list
      // a restarting gateway sends while it restores sessions. Nothing is done
      // while the list is partial; otherwise the switch asks the gateway, whose
      // 404 reports a deleted session gone and whose success keeps a live one.
      } else if (closePhase === null && !closeSeenRef.current && !listPartial) {
        switchAgain()
      }
      return
    }
    if (activeSlot === slotKey || (replacement && activeSlot === replacement)) return
    // Two such panels on different sessions take the active slot from each
    // other back to back, a gateway read each time. A run of quick switch-backs
    // stops at the cap for that session, showing no chat; one after a quiet gap
    // starts a new run, so a lone panel's occasional switch-backs never reach it.
    const backs = switchBacksRef.current
    if (backs.count > MAX_SWITCH_BACKS) return
    const now = Date.now()
    backs.count = now - backs.at < SWITCH_BACK_GAP_MS ? backs.count + 1 : 1
    backs.at = now
    if (backs.count > MAX_SWITCH_BACKS) {
      // eslint-disable-next-line no-console -- an app mounting two such panels has no other signal
      console.warn(`useOwnSlotChat: stopped switching back to session ${slotKey}: another embedded chat keeps making a different session active. Mount one conversationOnly ChatPanel at a time.`)
      return
    }
    switchAgain()
  }, [followsNavigation, switchingElsewhere, slots, activeSlot, slotKey, readyKey, closePhase, listPartial, onSessionGone, onSessionReplaced])

  // The chat renders the ACTIVE slot, so it is shown only while that slot is
  // the session or its memory-mode replacement -- or, with a sessions rail,
  // once the session has opened.
  const replacement = slotKey ? replacementOf(slotKey) : undefined
  const showChat = slotKey !== null && readyKey === slotKey && (followsNavigation
    || activeSlot === slotKey || (replacement !== undefined && activeSlot === replacement))

  return {
    showChat,
    failure,
    retry: () => { retryRef.current = true; setAttempt(n => n + 1) },
  }
}
