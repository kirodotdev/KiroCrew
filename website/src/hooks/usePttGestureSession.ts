/**
 * The session wiring shared by the two pointer-driven push-to-talk gestures:
 * the touch hold bar (`useTouchPushToTalk`) and press-and-hold on the message
 * box (`usePointerHoldToTalk`).
 *
 * Both build the same {@link createPttSession} core with the same policy -- no
 * latch, because nothing either gesture opens survives its own gesture, and
 * `disownPendingOnRelinquish` on, because a replacement session can come from
 * a surface the gesture never sees (the mic button, the keyboard binding) --
 * re-read the stored push-to-talk config when it changes, and clear their arm
 * timer together with the core's hard cap. Kept in one place so a fix to that
 * wiring cannot land in one gesture and miss the other.
 *
 * Owns no state of its own: every ref and setter is the caller's, so each
 * gesture keeps its own phase machine, and React sees the same hooks, in the
 * same order, that the callers ran inline before.
 */
import { useCallback, useEffect, useRef, type MutableRefObject } from 'react'

import { loadPttConfig, PTT_CHANGED_EVENT, type PttConfig } from '../lib/pushToTalk'
import {
  createPttSession,
  type PttPhase,
  type PttSession,
  type VoiceControls,
} from '../lib/pttSession'

/** Late-bound callbacks the core calls back into. The caller defines them in
 *  terms of the returned session, so it re-points them on every render. */
export interface PttGestureSeams {
  resetToIdle: () => void
  disarm: (commit: boolean) => void
}

export interface UsePttGestureSessionArgs {
  voiceRef: MutableRefObject<VoiceControls>
  phaseRef: MutableRefObject<PttPhase>
  setPhase: (p: PttPhase) => void
  coreSeamsRef: MutableRefObject<PttGestureSeams>
  armTimerRef: MutableRefObject<ReturnType<typeof setTimeout> | null>
  /** Mirrors ownership to render (the gesture's exported `owns`). */
  setOwns: (owns: boolean) => void
  /** Receives the stored config whenever another surface or tab saves it. */
  setCfg: (cfg: PttConfig) => void
}

export function usePttGestureSession({
  voiceRef, phaseRef, setPhase, coreSeamsRef, armTimerRef, setOwns, setCfg,
}: UsePttGestureSessionArgs): { session: PttSession<'gesture'>; clearTimers: () => void } {
  const sessionRef = useRef<PttSession<'gesture'> | null>(null)
  if (!sessionRef.current) {
    sessionRef.current = createPttSession<'gesture'>({
      voice: () => voiceRef.current,
      phase: () => phaseRef.current,
      setPhase,
      resetToIdle: () => { coreSeamsRef.current.resetToIdle() },
      disarm: (commit) => { coreSeamsRef.current.disarm(commit) },
      disownPendingOnRelinquish: true,
      onOwnerChange: (owner) => setOwns(owner !== null),
    })
  }
  const session = sessionRef.current

  useEffect(() => {
    const onChange = () => setCfg(loadPttConfig())
    window.addEventListener(PTT_CHANGED_EVENT, onChange)
    window.addEventListener('storage', onChange)
    return () => {
      window.removeEventListener(PTT_CHANGED_EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [setCfg])

  const clearTimers = useCallback(() => {
    if (armTimerRef.current) { clearTimeout(armTimerRef.current); armTimerRef.current = null }
    session.clearCapTimer()
  }, [session, armTimerRef])

  return { session, clearTimers }
}
