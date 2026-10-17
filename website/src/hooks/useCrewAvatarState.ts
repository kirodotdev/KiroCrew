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
import { packSoundUrl } from '../lib/appearancePacks/library'
import type { AvatarFaceState, AvatarSounds, AvatarState } from '../lib/crewAvatarState'
import { loadSoundSettings, playPreset, playSoundFile } from './useNotificationSound'

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
  /** The crew's per-state preset sounds. Absent or `'none'` = silent. This is
   *  the GHOST's cue: `soundsFrom` reads it on the ghost tier alone. */
  sounds?: AvatarSounds | null
  /**
   * The pack this crew wears, and the states its manifest declares a cue for.
   *
   * A pack brings its own audio, so it answers the cue question INSTEAD of the
   * presets rather than alongside them — a crew cannot report one moment with two
   * sounds. A state the pack does not declare is silent, and deliberately does
   * not fall back to a preset: the pack's author chose which moments make a
   * sound, and filling the gaps with a synthesized tone would report their pack
   * with a sound they left out.
   */
  packCue?: { id: string; states: Readonly<Record<string, boolean>> } | null
  /**
   * The crew wears a pack whose manifest has NOT arrived yet, so what it sounds
   * like is not yet knowable.
   *
   * Distinct from `packCue: null`, which means "no cue" — a crew wearing no
   * pack, or a pack that declares none. Without the distinction a turn that
   * finishes during the read resolves to silence AND consumes the transition, so
   * the cue never plays for that turn even once the manifest lands.
   */
  packPending?: boolean
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
  packCue,
  packPending,
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
    // A pack whose manifest has not arrived cannot answer the cue question yet,
    // and advancing the baseline would CONSUME the transition: the effect would
    // not run again as an edge once the manifest landed, so a turn that finished
    // during the read would be silent for good. Holding it costs nothing — the
    // effect re-runs when `packCue` resolves and sees the same edge still
    // pending.
    //
    // The hold lasts exactly as long as the REACTION IS ON SCREEN, and that bound
    // is deliberate rather than emergent: when the flash dwell ends, `state`
    // returns to `idle`, the baseline advances there, and a manifest arriving
    // afterwards finds no edge. A read slower than the dwell therefore loses that
    // turn's cue — which is the outcome to want, because a chime arriving seconds
    // after the face has gone back to rest is a sound with nothing on screen to
    // explain it, and the next turn would inherit it. Pinned by "does not fire a
    // stale edge once the flash has passed".
    //
    // The other consequence of holding rather than queueing: the cue that fires
    // when the manifest lands is for the state the crew is in THEN. A state it
    // passed through during the read — a short turn whose `working` became `done`
    // before the manifest arrived — is not replayed, because two cues for one
    // moment would report the crew twice. Pinned by "plays only the current
    // state's cue when the manifest lands after a superseded edge".
    if (packPending && state !== 'idle') return
    previousState.current = state
    // Same baseline rule as the face: mounting into a running crew is not an
    // entry into `working`, and page load must be silent.
    if (previous === null || previous === state || state === 'idle') return
    // A worn pack owns the cue outright — see `packCue`. Resolved BEFORE the
    // gate and the debounce so both halves share them: whichever source answers,
    // the global toggle silences it and one crew cannot chime twice for one edge.
    const play = cueFor(state, packCue, sounds)
    if (!play) return
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
    play(settings.volume)
  }, [state, sounds, packCue, packPending, identity])

  return state
}

/**
 * What to play for this state, or null for silence — one decision, so the gate
 * and the debounce below it cannot disagree with it.
 *
 * A pack SHORT-CIRCUITS: a crew wearing one is silent on a state the pack does
 * not declare rather than falling through to a preset, because the pack is the
 * answer to "what does this crew sound like" once it is worn.
 */
function cueFor(
  state: AvatarState,
  packCue: CrewAvatarStateOptions['packCue'],
  sounds: AvatarSounds | null | undefined,
): ((volume: number) => void) | null {
  if (packCue) {
    if (!packCue.states[state]) return null
    const url = packSoundUrl(packCue.id, state)
    return volume => playSoundFile(url, volume)
  }
  const preset = sounds?.[state]
  if (!preset || preset === 'none') return null
  return volume => playPreset(preset, volume)
}
