import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChatMessage, TurnIdentityFields } from '../../types'
import { awaitingCrewmateReply } from './crewmateBubbles'
import type { PillActivity } from '../../pages/members/pillActivity'

/** The caption while a send waits for the turn that will answer it: the
 *  pill's own `working` phase (no new wording), never the slot's status record,
 *  which still names the last turn's step. */
export const AWAITING_REPLY_ACTIVITY: PillActivity = { kind: 'working' }

/**
 * Whether a crewmate chat's newest send from this pane still waits for the
 * crewmate, so the typing bubble shows from the moment the person presses
 * Send rather than only once the turn that answers has started
 * (`awaitingCrewmateReply` is the row rule). The pane's composer reads it as
 * busy too, and Resume is withheld while it holds: the person's own row is
 * the newest one, which reads as an interrupted turn until the reply lands.
 *
 * `markSent` records the send; it is bound to the slot it was typed into, so a
 * pane rebound to another crewmate waits on nothing. A live turn that ends
 * while the send is NOT parked in the queue ends the wait too: that turn was
 * the one that took the send and it wrote no row for it. A turn that ends while
 * the send is still queued is the one before it, so the wait goes on.
 *
 * `turn` is the slots frame's turn identity for the slot. A frame naming a
 * different turn than the one current at Send, while the slot is not live,
 * ends the wait the same way: the turn that took the send began and ended
 * between two frames (the frames coalesce, so this pane never saw it running),
 * or the gateway restarted (`turn_gen` moved). Otherwise that wait would never
 * end, and the composer would keep a Stop with nothing to stop where Resume
 * belongs.
 */
export function useCrewmateAwaitingReply(
  enabled: boolean,
  slotKey: string,
  all: ChatMessage[],
  live: boolean,
  turn?: TurnIdentityFields,
): { awaitingReply: boolean; markSent: (sendId: string) => void } {
  const [awaited, setAwaited] = useState<{ slot: string; sendId: string; turn?: number; gen?: string } | null>(null)
  const wasLive = useRef(live)
  const turnNo = turn?.turn
  const turnGen = turn?.turn_gen
  // Read at Send time by `markSent`, which stays stable across frames.
  const turnAtSend = useRef({ turn: turnNo, gen: turnGen })
  turnAtSend.current = { turn: turnNo, gen: turnGen }
  const lastTurn = useRef({ turn: turnNo, gen: turnGen })
  useEffect(() => {
    const fell = wasLive.current && !live
    wasLive.current = live
    // Like `fell`, an edge: only the frame that names a new turn decides.
    // Re-read on every row change, a turn that ended while this send was
    // still queued would end the wait at the drain, before its own turn ran.
    const turnChanged = lastTurn.current.turn !== turnNo || lastTurn.current.gen !== turnGen
    lastTurn.current = { turn: turnNo, gen: turnGen }
    if (!awaited) return
    // Only the slot the send was typed into: a pane re-pointed at another
    // crewmate reads THAT slot's turn, which says nothing about this send.
    const moved = turnChanged && !live && awaited.slot === slotKey && awaited.turn !== undefined && turnNo !== undefined
      && (turnGen !== awaited.gen || turnNo !== awaited.turn)
    if (!fell && !moved) return
    if (!all.some(m => m.role === 'queued' && m.meta?.sendId === awaited.sendId)) setAwaited(null)
  }, [live, awaited, all, turnNo, turnGen, slotKey])
  const markSent = useCallback(
    (sendId: string) => setAwaited({ slot: slotKey, sendId, ...turnAtSend.current }),
    [slotKey],
  )
  const awaitingReply = enabled && awaited?.slot === slotKey && awaitingCrewmateReply(all, awaited.sendId)
  return { awaitingReply, markSent }
}
