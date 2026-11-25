import { useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef } from 'react'
import type { SteerEscape } from '../components/chat-input/steerEscape'
import type { ChatMessage } from '../types'
import { canTakeBackIntoComposer, type SteerRetract } from './useSteerRetract'

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
  /** Whether this slot's history fetch has settled. While it has not, a queued
   *  press whose row is missing waits instead of being dropped. Defaults true. */
  transcriptAuthoritative?: boolean
  /** A turn is running on the slot. Its pending steers exist only while it runs. */
  slotRunning: boolean
  /** The slot's stop state. A hard stop discards the pending steers, so none is
   *  taken back while it runs. */
  stopState?: string
  /** The slot's turn-start count (`selectSlotRunEpoch`): presses belong to one turn. */
  runEpoch?: number
  onSteerAction: SteerRetract['onSteerAction']
  /** The take-back in flight for each row, from `useSteerRetract`. */
  pending: SteerRetract['pending']
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
 * A refused take-back drops the presses queued behind it: the row says what
 * happened, and the next press asks again. A take-back whose answer is lost holds
 * them until its row settles, since the gateway may or may not hold it.
 */
export function useSteerEscape({ slot, messages, transcriptAuthoritative = true, slotRunning, stopState, runEpoch = 0, onSteerAction, pending }: SteerEscapeOptions): SteerEscape {
  const latest = useRef({ slot, messages, transcriptAuthoritative, slotRunning, stopState, onSteerAction, pending })
  latest.current = { slot, messages, transcriptAuthoritative, slotRunning, stopState, onSteerAction, pending }
  const queueRef = useRef<SteerTarget[]>([])
  // The take-back sent and not yet settled. `unanswered` once its request failed
  // before its outcome was known: the take-back hook keeps that record until the
  // row settles, and the claim stands as long as the record does.
  const inFlightRef = useRef<{ target: SteerTarget; unanswered: boolean } | null>(null)
  // Rows whose take-back ended without being armed: the route said the steer was
  // no longer pending, or refused it, or nothing could be sent. The row may still
  // read as a target (its own update can arrive after the answer, and a refusal
  // leaves it as it was), so until the row moves on Escape leaves it alone.
  const settledRef = useRef<Set<string>>(new Set())
  // Bumped by every reset, so an answer for a slot or turn that is gone is ignored.
  const generationRef = useRef(0)
  const [claimsVersion, claimsChanged] = useReducer((n: number) => n + 1, 0)

  const reset = useCallback(() => {
    if (!queueRef.current.length && !inFlightRef.current && !settledRef.current.size) return
    generationRef.current += 1
    queueRef.current = []
    inFlightRef.current = null
    settledRef.current = new Set()
    claimsChanged()
  }, [])

  const pump = useCallback(() => {
    const now = latest.current
    // A hard stop discards the pending steers, so nothing is left to take back.
    if (!now.slotRunning || now.stopState === 'killing') { reset(); return }
    if (settledRef.current.size) {
      for (const mid of settledRef.current) {
        const row = now.messages.find(m => isRowOf(m, { mid }))
        if (!row || !targetOf(row)) settledRef.current.delete(mid)
      }
    }
    if (inFlightRef.current) {
      const held = inFlightRef.current
      if (!held.unanswered || ('mid' in held.target && held.target.mid in now.pending)) return
      inFlightRef.current = null
    }
    while (queueRef.current.length) {
      // One take-back per chat at a time: the next waits until this one settles.
      if (Object.keys(now.pending).length) return
      if (!now.transcriptAuthoritative) return
      const row = now.messages.find(m => isRowOf(m, queueRef.current[0]))
      const target = row ? targetOf(row) : null
      if (target && 'sendId' in target) return
      queueRef.current.shift()
      claimsChanged()
      if (!target) continue
      const generation = generationRef.current
      const sentFor = now.slot
      const held = { target, unanswered: false }
      inFlightRef.current = held
      void now.onSteerAction(target.mid, 'edit', { prepend: true, retryBusy: true }).then(outcome => {
        if (generationRef.current !== generation || latest.current.slot !== sentFor || inFlightRef.current !== held) return
        if (outcome === 'unknown') {
          held.unanswered = true
          return
        }
        inFlightRef.current = null
        if (outcome === 'accepted' || outcome === 'not_pending') {
          if (outcome === 'not_pending') settledRef.current.add(target.mid)
          pump()
          return
        }
        settledRef.current.add(target.mid)
        queueRef.current = []
        claimsChanged()
      })
      return
    }
  }, [reset])

  // In the commit that shows another chat or turn, before any answer for the old one can land.
  useLayoutEffect(() => { reset() }, [slot, runEpoch, reset])
  // An answer that lands after the composer is gone changes nothing.
  useEffect(() => () => { generationRef.current += 1 }, [])
  useEffect(() => { pump() }, [messages, transcriptAuthoritative, pending, slotRunning, stopState, pump])

  const claimed = useCallback((m: ChatMessage, inFlight: Readonly<Record<string, unknown>>) => {
    const mid = (m.meta as { mid?: unknown } | undefined)?.mid
    if (typeof mid === 'string' && (mid in inFlight || settledRef.current.has(mid))) return true
    const asked = inFlightRef.current ? [inFlightRef.current.target, ...queueRef.current] : queueRef.current
    return asked.some(target => isRowOf(m, target))
  }, [])

  const takeBack = useCallback((): boolean => {
    const now = latest.current
    if (!now.slot || !now.slotRunning || now.stopState === 'killing' || !canTakeBackIntoComposer()) return false
    const reachable = now.messages.filter(m => targetOf(m) && !claimed(m, now.pending))
    const newest = reachable[reachable.length - 1]
    const target = newest && targetOf(newest)
    if (!target) return false
    queueRef.current.push(target)
    claimsChanged()
    pump()
    return true
  }, [claimed, pump])

  const counts = useMemo(() => {
    // The queue lives in refs, so `claimsVersion` is what re-runs this count when it changes.
    void claimsVersion
    if (!slot || !slotRunning || stopState === 'killing' || !canTakeBackIntoComposer()) return { offered: 0, reachable: 0 }
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

  // Keyed on the two numbers, so a streamed chunk that changes neither leaves the
  // composer's props as they were.
  const { offered, reachable } = counts
  return useMemo(() => ({ offered, reachable, takeBack }), [offered, reachable, takeBack])
}
