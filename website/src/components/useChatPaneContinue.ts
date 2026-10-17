import { useState, useCallback, useEffect, useMemo } from 'react'
import { useAppSelector } from '../store'
import { selectContinuable, selectTurnInterrupted } from '../store/chatSlice'
import { newestErrorOwnsRecovery } from '../pages/chat/transcriptRenderers'
import { useContinuePress } from '../hooks/useContinuePress'
import type { ChatMessage } from '../types'

/**
 * The pane's side of Resume: whether the composer and the newest error row may
 * offer it, and the in-pane notice when the gateway refuses it. The press and
 * its spinner are `useContinuePress`, the one ChatPage's `useTurnRecovery` uses
 * too. ChatPage asks the store about the ACTIVE slot. A pane shows any slot (a
 * crewmate's chat on the Members page never is the active one), so it hands its
 * own key to the slot-aware selectors.
 *
 * `continuable` (a continuation is safe: nothing running, stopping or queued)
 * and `interrupted` (the transcript shows the turn ending badly) stay separate:
 * the error row takes both raw and applies its own per-row remedies
 * (`createTranscriptRenderers`), while the composer takes `composerResume`, the
 * two composed with the cases where the newest error card withholds Resume.
 *
 * `sendPending` is the pane's own optimistic local turn: a send that still
 * waits for the turn that answers it. ChatPage's send marks that turn in the
 * store (`startLocalTurn`), which only the active slot can carry, so a pane
 * hands it in instead. Otherwise the person's own confirmed row, newest and
 * unanswered, reads as an interrupted turn until the slot is seen running.
 */
export function useChatPaneContinue({ slotKey, messages, turnRunning, sendPending = false }: {
  slotKey: string
  /** The rows the pane draws. */
  messages: ChatMessage[]
  /** A turn is running on the slot: the press took, or an old refusal is over. */
  turnRunning: boolean
  /** This pane's send still waits for the turn that answers it. */
  sendPending?: boolean
}) {
  const storeContinuable = useAppSelector((s) => selectContinuable(s, slotKey))
  const continuable = storeContinuable && !sendPending
  const interrupted = useAppSelector((s) => selectTurnInterrupted(s, slotKey))
  // The newest error row has a remedy of its own and has withheld Resume, since
  // a retry replays the rejection. The composer must not urge the press beneath
  // that card; typing still works. The rule is ChatPage's own, one predicate.
  // Memoised: the pane re-renders per keystroke.
  const cardOwnsRecovery = useMemo(() => newestErrorOwnsRecovery(messages), [messages])
  const [continueError, setContinueError] = useState('')
  const { continuing, press } = useContinuePress({
    slot: slotKey,
    turnRunning,
    continuable,
    onRefused: (e) => setContinueError(e instanceof Error && e.message ? e.message : String(e)),
  })
  // The notice is about ONE slot (the Members page re-points a mounted pane at
  // another crewmate), and a turn starting retires it: the state it described
  // has passed. ChatPage does the same for its shared refusal notice.
  useEffect(() => { setContinueError('') }, [slotKey])
  useEffect(() => { if (turnRunning) setContinueError('') }, [turnRunning])
  const onContinue = useCallback(() => {
    setContinueError('')
    press()
  }, [press])

  return {
    continuable,
    interrupted,
    composerResume: continuable && interrupted && !cardOwnsRecovery,
    continuing,
    onContinue,
    continueError,
    setContinueError,
  }
}
