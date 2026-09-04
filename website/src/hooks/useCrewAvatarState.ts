/**
 * Which reaction a crew's avatar is showing right now.
 *
 * The signal is the crew's own session slot, not a new backend channel:
 * `running` is what MembersPage already reads to light its presence dot, and
 * `interrupted` is what the composer already reads to offer Resume ("the last
 * turn ended without a reply"). So `working` is simply "the slot is running",
 * and the moment it stops is the only edge that exists — `error` when the
 * transcript ended badly, `done` when it did not.
 *
 * `done` and `error` are FLASHES, not states: nothing on the wire says "this
 * crew finished four seconds ago", so the finish is an edge and the dwell is
 * ours. It is short on purpose — a permanent badge would turn every idle crew
 * into a wall of verdicts from whenever it last ran, including from before the
 * page was opened.
 *
 * Which is also why the first observation is never a transition. On mount the
 * slot arrives already running or already idle, and treating that as an edge
 * would flash (and chime) for every crew in the roster on every page load,
 * reporting turns that finished hours ago.
 */
import { useEffect, useRef, useState } from 'react'

import { useAppSelector } from '../store'
import type { AvatarFaceState, AvatarSounds } from '../lib/crewAvatarState'
import { loadSoundSettings, playPreset } from './useNotificationSound'

/** How long `done` / `error` shows before the face returns to rest. */
export const AVATAR_FLASH_MS = 4000

/**
 * One sound per crew per state inside this window.
 *
 * Two things need it, and neither is hypothetical. A `running` flag that
 * flaps — a burst of short turns, or a slots frame that momentarily disagrees
 * with the one before it — would otherwise play a tone per flip. And the
 * SAME crew is commonly on screen twice (a roster row and the open thread's
 * header), which is two hook instances observing one edge; the map is
 * module-level precisely so the second one stays silent.
 */
export const AVATAR_SOUND_WINDOW_MS = 1500

/** Bounded: keyed by slot + state, and cleared wholesale rather than evicted
 *  per entry, because the map holds only a debounce timestamp — losing it
 *  costs at most one extra tone. */
const MAX_TRACKED_SLOTS = 500
const lastPlayedAt = new Map<string, number>()

/** Test-only helper: the debounce map outlives an individual render tree. */
export function __resetCrewAvatarSoundForTests(): void {
  lastPlayedAt.clear()
}

/** Monotonic-ish clock that also works where `performance` is stubbed away. */
function now(): number {
  return typeof performance !== 'undefined' ? performance.now() : Date.now()
}

export interface CrewAvatarStateOptions {
  /** The crew's session slot. Preferred: the caller usually already resolved
   *  it (MembersPage keys its whole roster on it). */
  slotKey?: string | null
  /** Crew name, for a surface that has no slot key — the crew editor, which
   *  knows which crew it is editing but not which session it drives. Resolves
   *  to that crew's live member slot, if it has one. */
  agentName?: string | null
  /** Authoritative running flag, when the caller has a better one than the
   *  live slot alone: MembersPage falls back to the roster endpoint's snapshot
   *  before the first slots frame arrives. Omitted, the live slot decides. */
  running?: boolean
  /** The crew's per-state sounds. Absent or `'none'` for a state = silent. */
  sounds?: AvatarSounds | null
}

/** A flash plus the edge that produced it: two finishes in a row carry the
 *  same verdict, and without the sequence number the second would not restart
 *  the dwell (React skips a state write that changes nothing). */
interface Flash {
  state: 'done' | 'error'
  seq: number
}

export function useCrewAvatarState({
  slotKey,
  agentName,
  running,
  sounds,
}: CrewAvatarStateOptions): AvatarFaceState {
  // A crew's member slot, when the caller passed a name instead of a key.
  // Returns a string, so this selector cannot churn on slot-array identity.
  const resolvedKey = useAppSelector((s) => {
    if (slotKey) return slotKey
    if (!agentName) return ''
    for (const slot of s.dashboard.slots) {
      if (slot.mode === 'member' && slot.agent === agentName) return slot.key
    }
    return ''
  })
  const liveRunning = useAppSelector((s) =>
    resolvedKey ? !!s.dashboard.slots.find((x) => x.key === resolvedKey)?.running : false,
  )
  // `interrupted` is the backend's own reading of the transcript and is always
  // false while running, so it is only ever consulted on the stopping edge.
  const interrupted = useAppSelector((s) =>
    resolvedKey ? !!s.dashboard.slots.find((x) => x.key === resolvedKey)?.interrupted : false,
  )

  const isWorking = running ?? liveRunning
  // A crew with no session yet has no slot key, and every such crew would
  // otherwise share one debounce entry — one of them finishing would silence
  // the rest. The name is the fallback identity.
  const identity = resolvedKey || agentName || ''
  const [flash, setFlash] = useState<Flash | null>(null)
  /** null = nothing observed yet, so the next reading is a baseline. */
  const wasWorking = useRef<boolean | null>(null)
  /** Same baseline rule for the sound, which rides the resolved state. */
  const previousState = useRef<AvatarFaceState | null>(null)
  const seq = useRef(0)

  // Switching which crew this instance watches restarts the observation: the
  // new crew's first reading is a baseline, not an edge, and the previous
  // crew's flash must not be attributed to it. Declared BEFORE the edge and
  // sound effects so it runs first within the same commit.
  //
  // `previousState` is reset here too, and that is not symmetry for its own
  // sake: the editor header and the roster's open thread REUSE one instance
  // across crews, so selecting an already-running crew moves `state` from the
  // previous crew's idle to this one's working with no transition behind it —
  // and the sound would fire for a turn that started before the crew was even
  // selected.
  useEffect(() => {
    wasWorking.current = null
    previousState.current = null
    setFlash(null)
  }, [identity])

  useEffect(() => {
    const previous = wasWorking.current
    wasWorking.current = isWorking
    if (previous === null || previous === isWorking) return
    if (isWorking) {
      // Started again: working outranks a flash still on screen.
      setFlash(null)
      return
    }
    seq.current += 1
    setFlash({ state: interrupted ? 'error' : 'done', seq: seq.current })
  }, [isWorking, interrupted])

  useEffect(() => {
    if (!flash) return
    const timer = window.setTimeout(() => setFlash(null), AVATAR_FLASH_MS)
    return () => window.clearTimeout(timer)
  }, [flash])

  const state: AvatarFaceState = isWorking ? 'working' : (flash?.state ?? 'idle')

  // Sound rides the RESOLVED state rather than the raw running flag, so the
  // tone and the face always agree about which moment this is.
  useEffect(() => {
    const previous = previousState.current
    previousState.current = state
    // Same baseline rule as the face: mounting into a running crew is not an
    // entry into `working`, and page load must be silent.
    if (previous === null || previous === state || state === 'idle') return
    const preset = sounds?.[state]
    if (!preset || preset === 'none') return
    // Read fresh: the global toggle and volume live in localStorage and the
    // user may have changed them since this component mounted.
    const settings = loadSoundSettings()
    if (!settings.enabled || settings.volume <= 0) return
    // NUL-joined rather than interpolated, the same idiom CrewAvatar's cache
    // key uses: the separator cannot occur in either part.
    const key = [identity, state].join('\u0000')
    const at = now()
    if (at - (lastPlayedAt.get(key) ?? Number.NEGATIVE_INFINITY) < AVATAR_SOUND_WINDOW_MS) return
    if (lastPlayedAt.size >= MAX_TRACKED_SLOTS) lastPlayedAt.clear()
    lastPlayedAt.set(key, at)
    playPreset(preset, settings.volume)
  }, [state, sounds, identity])

  return state
}
