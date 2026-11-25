import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import type { SteerEscape, SteerEscapeVerdict } from '../components/chat-input/stopKey'
import type { ChatMessage } from '../types'
import type { SteerRetract } from './useSteerRetract'

/** A steer one Escape press asked to take back: a written row by its id, or the
 *  composer's own bubble by its send id while the steer's echo has not given it one. */
type SteerTarget = { mid: string } | { sendId: string }

/** The row as a take-back target, or null when Escape cannot reach it.
 *
 *  Escape reaches two kinds of row: a `written` row that has its id and offers a
 *  take-back (`steerRetract`), and the composer's optimistic bubble, which has
 *  neither until its echo arrives. */
function targetOf(m: ChatMessage): SteerTarget | null {
  const meta = m.meta as Record<string, unknown> | undefined
  if (m.role !== 'user' || meta?.steer !== true) return null
  const mid = typeof meta.mid === 'string' ? meta.mid : ''
  if (mid) return meta.steerState === 'written' && meta.steerRetract === true ? { mid } : null
  const sendId = typeof meta.sendId === 'string' ? meta.sendId : ''
  return meta.optimistic === true && !meta.steerState && sendId ? { sendId } : null
}

function isRowOf(m: ChatMessage, target: SteerTarget): boolean {
  const meta = m.meta as Record<string, unknown> | undefined
  return 'mid' in target ? meta?.mid === target.mid : meta?.sendId === target.sendId
}

export interface SteerEscapeOptions {
  /** The slot whose transcript `messages` is. */
  slot: string | null
  messages: ChatMessage[]
  /** A turn is running on the slot. Its pending steers exist only while it runs. */
  slotRunning: boolean
  /** The slot's stop state. A hard stop discards the pending steers, so none is
   *  taken back while it runs. */
  stopState?: string
  onSteerAction: SteerRetract['onSteerAction']
  /** The take-back in flight for each row, from `useSteerRetract`. */
  pending: SteerRetract['pending']
  /** The composer's Stop press. */
  stop: () => void
}

/**
 * The composer's Escape on the steers the running turn has not read yet.
 *
 * Each press takes back the newest pending steer no press has asked for yet, with
 * Edit, and its text goes in front of the composer's draft: pressing once per
 * steer rebuilds them in the order they were sent, above the draft. The gateway
 * runs one take-back at a time per chat, so presses queue here and each is sent
 * once the one before it has settled on its row.
 *
 * A press can arrive while the composer's own steer still waits for its echo, so
 * the bubble has no id to take back by. The press is kept and sent when the echo
 * names the row, and dropped when the row turns out not to offer a take-back.
 *
 * The stop Escape asks for waits for every take-back queued here to be armed.
 * A turn that ends with a steer still pending runs that steer as the next turn,
 * so a stop sent first would run the very message the press took back. A
 * take-back that is refused, or whose answer is lost, drops the queue and the
 * stop: the row says what happened, and the next press asks again.
 */
export function useSteerEscape({ slot, messages, slotRunning, stopState, onSteerAction, pending, stop }: SteerEscapeOptions): SteerEscape {
  const latest = useRef({ slot, messages, slotRunning, stopState, onSteerAction, pending, stop })
  latest.current = { slot, messages, slotRunning, stopState, onSteerAction, pending, stop }
  const queueRef = useRef<SteerTarget[]>([])
  const inFlightRef = useRef<SteerTarget | null>(null)
  const stopOwedRef = useRef(false)
  // Bumped by every reset, so an answer for a slot or turn that is gone is ignored.
  const generationRef = useRef(0)
  const [claimsVersion, claimsChanged] = useReducer((n: number) => n + 1, 0)

  const reset = useCallback(() => {
    if (!queueRef.current.length && !inFlightRef.current && !stopOwedRef.current) return
    generationRef.current += 1
    queueRef.current = []
    inFlightRef.current = null
    stopOwedRef.current = false
    claimsChanged()
  }, [])

  const pump = useCallback(() => {
    const now = latest.current
    if (!now.slotRunning) { reset(); return }
    if (inFlightRef.current) return
    while (queueRef.current.length) {
      // One take-back per chat at a time: the next waits until this one settles.
      if (Object.keys(now.pending).length) return
      const row = now.messages.find(m => isRowOf(m, queueRef.current[0]))
      const target = row ? targetOf(row) : null
      if (target && 'sendId' in target) return
      queueRef.current.shift()
      claimsChanged()
      if (!target) continue
      const generation = generationRef.current
      inFlightRef.current = target
      void now.onSteerAction(target.mid, 'edit', { prepend: true, retryBusy: true }).then(outcome => {
        if (generationRef.current !== generation) return
        inFlightRef.current = null
        if (outcome === 'accepted' || outcome === 'not_pending') {
          pump()
          return
        }
        queueRef.current = []
        stopOwedRef.current = false
        claimsChanged()
      })
      return
    }
    if (stopOwedRef.current) {
      stopOwedRef.current = false
      now.stop()
    }
  }, [reset])

  useEffect(() => { reset() }, [slot, reset])
  useEffect(() => { pump() }, [messages, pending, slotRunning, pump])

  const claimed = useCallback((m: ChatMessage, inFlight: Readonly<Record<string, unknown>>) => {
    const mid = (m.meta as { mid?: unknown } | undefined)?.mid
    if (typeof mid === 'string' && mid in inFlight) return true
    const asked = inFlightRef.current ? [inFlightRef.current, ...queueRef.current] : queueRef.current
    return asked.some(target => isRowOf(m, target))
  }, [])

  const takeBack = useCallback((): SteerEscapeVerdict => {
    const now = latest.current
    if (!now.slot || !now.slotRunning || now.stopState === 'killing') return 'none'
    const reachable = now.messages.filter(m => targetOf(m) && !claimed(m, now.pending))
    const newest = reachable[reachable.length - 1]
    const target = newest && targetOf(newest)
    if (!target) return 'none'
    queueRef.current.push(target)
    claimsChanged()
    pump()
    return reachable.length === 1 ? 'last' : 'more'
  }, [claimed, pump])

  const stopAfterTakeBacks = useCallback(() => {
    if (queueRef.current.length || inFlightRef.current) {
      stopOwedRef.current = true
      return
    }
    latest.current.stop()
  }, [])

  const counts = useMemo(() => {
    // The queue lives in refs, so `claimsVersion` is what re-runs this count when it changes.
    void claimsVersion
    if (!slot || !slotRunning || stopState === 'killing') return { offered: 0, reachable: 0 }
    let offered = 0
    let reachable = 0
    for (const m of messages) {
      const target = targetOf(m)
      if (!target || claimed(m, pending)) continue
      reachable += 1
      if ('mid' in target) offered += 1
    }
    return { offered, reachable }
  }, [slot, slotRunning, stopState, messages, pending, claimed, claimsVersion])

  return useMemo(
    () => ({ offered: counts.offered, reachable: counts.reachable, takeBack, stop: stopAfterTakeBacks }),
    [counts, takeBack, stopAfterTakeBacks],
  )
}
