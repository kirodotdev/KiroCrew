/**
 * The session-ownership core shared by the push-to-talk transports.
 *
 * `usePushToTalk` (keyboard) and `useTouchPushToTalk` (touch) drive the same
 * contract: ONE capture session is opened the moment a gesture begins — before
 * anyone knows whether the press will turn out to be a tap, a hold, or a chord
 * — and ownership, not intent-at-open, decides that session's fate when the
 * gesture RESOLVES. This module owns that machinery: who owns the session
 * (`owner`), whether its async startup is still in flight (`startPending`), the
 * per-startup sequence that lets a late resolution tell "the session I opened"
 * from "a session someone else opened after me", and the gesture-generation
 * counter plus the hard-cap timer behind `beginHold`.
 *
 * Each transport used to carry its own copy of this state machine, and the
 * copies drifted: a fix applied to one silently missed the other. The
 * transports differ only where keyboard and touch genuinely differ, and each
 * difference is a construction-time parameter — see {@link PttSessionDeps}.
 *
 * Framework-free on purpose: the transports read and write it synchronously
 * from DOM event handlers, and unit tests drive it without `renderHook` or a
 * microphone.
 */
import { MAX_HOLD_MS } from './pushToTalk'

/**
 * The slice of `useVoiceInput` the push-to-talk transports drive.
 *
 * Injected rather than imported so the transports are testable without a
 * microphone, and read through a getter at call time so a re-render's new
 * callback identities are always the ones invoked.
 */
export interface VoiceControls {
  recording: boolean
  start: () => Promise<void> | void
  stop: () => void
  /**
   * End capture WITHOUT transcribing, and release whatever was acquired.
   *
   * This is the discard for a press that never became a recording. On the
   * streaming path it also drops the locally-buffered PCM, which is why a
   * discarded press transmits nothing.
   */
  cancel: () => void
}

/** The gesture phase both transports share. */
export type PttPhase = 'idle' | 'arming' | 'holding'

/**
 * What a transport provides the core — the seams where keyboard and touch
 * genuinely differ, plus live reads of the state the transport still owns.
 */
export interface PttSessionDeps<Owner extends string> {
  /** Live voice controls, read at call time, never captured. */
  voice: () => VoiceControls
  /** Current gesture phase — the settle handler's liveness test. */
  phase: () => PttPhase
  setPhase: (p: PttPhase) => void
  /**
   * Return the transport to idle after a FAILED startup: clear its timers,
   * bump the generation, and reset per-transport gesture state (touch's
   * cancel-zone flag and captured pointer id live here). The core resets the
   * phase through this rather than through `disarm`: with no session left
   * there is nothing to commit on a later release, and leaving the phase at
   * `arming`/`holding` would let the release path try.
   */
  resetToIdle: () => void
  /**
   * Leave any armed/holding state, committing (`stop`) or discarding as told —
   * the transport's own teardown. The hard-cap timer calls it when a release
   * is never heard about, which must not hold the mic forever.
   */
  disarm: (commit: boolean) => void
  /**
   * True for an owner kind that must OUTLIVE its gesture — the keyboard's
   * tap-latch / toggle. The settle handler leaves a latched session running
   * even though the phase is back to idle. Absent: no owner outlives its
   * gesture.
   */
  isLatched?: (owner: Owner) => boolean
  /**
   * When true, relinquishing ownership also DISOWNS any startup still in
   * flight by invalidating its sequence, so a late settle or rejection is a
   * no-op. Touch needs this: a replacement session there can come from the mic
   * button (a surface the gesture machinery never sees), whose sequence it
   * cannot distinguish from its own — without the disown, an abandoned
   * gesture's late-resolving startup called `stop()` on the replacement and
   * truncated speech it had nothing to do with. The orphan case stays covered
   * even with the settle handler silenced: every teardown calls `cancel()` or
   * `stop()` on the transport synchronously, and `useVoiceInput.cancel()`
   * bumps its OWN generation so the abandoned `start()` returns early and
   * never claims anything. The keyboard deliberately leaves the sequence
   * alone, so a startup that ignores teardown and goes live anyway is still
   * caught and stopped by its settle handler.
   */
  disownPendingOnRelinquish?: boolean
  /**
   * Observe every ownership change. Touch exports ownership to render
   * (`owns`), and a plain field cannot be exported — reading it during render
   * is unobservable — so the mirror is written here, by the sole writer.
   */
  onOwnerChange?: (owner: Owner | null) => void
}

/** The core's surface. One instance serves one transport for its whole life. */
export interface PttSession<Owner extends string> {
  /**
   * Open a session for the gesture that just began, and track the pending
   * startup so both a late resolution and a second press can reach it. EVERY
   * `start()` in a transport goes through here — a call site that skips it
   * leaves no way to reach its own startup.
   */
  launch(owner: Owner): void
  /**
   * The threshold passed, so the press is a HOLD. Capture has been running
   * since the gesture began — this only relabels the phase and arms the
   * ceiling. There is no `start()` here: a second one would be swallowed by
   * `useVoiceInput`'s re-entrancy guard, and the session opened at the
   * gesture's start is the one that already has the opening word in it.
   */
  beginHold(): void
  /**
   * Who owns the open session right now, or null when nobody does.
   *
   *   a gesture owner — the press that opened it. Owns it while the phase is
   *   `arming` or `holding`; once the phase returns to `idle` with this owner
   *   still set, the session is an ORPHAN.
   *   a latched owner — deliberately outlives its gesture, so an idle phase is
   *   expected and must not stop it.
   *
   * Ownership is written when the gesture RESOLVES, and every teardown clears
   * it, which is what lets the settle handler tell "still wanted" from
   * "nothing is holding this any more".
   */
  owner(): Owner | null
  /**
   * Hand ownership over (latch adoption) or relinquish it (teardown). The
   * ONLY writer — state cleared on some exit paths and not others is the
   * exact defect class the transports have already been fixed for.
   */
  setOwner(owner: Owner | null): void
  /**
   * True while `start()`'s async startup is in flight.
   *
   * Load-bearing for the stuck-mic guarantee, because a startup can fail to
   * settle AT ALL: the streaming path awaits a `ready` frame from the backend,
   * and a socket that opens and then goes silent leaves that await pending
   * forever. Any cleanup that runs only in the promise's own `.then()`
   * therefore inherits its liveness — so every teardown in this window runs
   * SYNCHRONOUSLY off this flag instead of awaiting anything.
   *
   * It is also the only way to know a session exists at all before it goes
   * live: `voice.recording` stays false for the whole `getUserMedia` +
   * handshake window, so a second press that tested only `recording` fell
   * through and opened a SECOND `start()` — which `useVoiceInput`'s
   * re-entrancy guard swallowed, leaving the first startup to go live against
   * a user who had just pressed to switch it off.
   */
  startPending(): boolean
  /**
   * Current gesture generation. Touch reads it to guard its arm timer, whose
   * callback must not promote a LATER press's `arming` phase to a hold. The
   * keyboard's arm timer needs no such guard — its handler nulls the timer
   * ref and checks the phase, and every keyboard path that could start a new
   * press first runs `clearTimers`, so a stale keyboard arm timer cannot
   * exist.
   */
  generation(): number
  /**
   * Invalidate timers armed by an earlier gesture, so one cannot fire against
   * a later one. Deliberately NOT the guard for the async `start()`
   * resolution — teardown bumping it is exactly what made that guard dead;
   * the settle handler's liveness test is the PHASE instead.
   */
  bumpGeneration(): number
  /** Clear the hard-cap timer — part of the transport's own clearTimers. */
  clearCapTimer(): void
}

export function createPttSession<Owner extends string>(
  deps: PttSessionDeps<Owner>,
): PttSession<Owner> {
  let owner: Owner | null = null
  let startPending = false
  /**
   * Monotonic per-`start()` sequence, bumped at every launch (and, under
   * `disownPendingOnRelinquish`, by any teardown that supersedes a pending
   * startup). Lets a late-resolving startup tell "the session I opened" from
   * "a session someone else opened after me" — without it, the settle
   * handler's phase test is an unconditional "not mine" and stops whatever is
   * live, killing a session the user opened inside the `getUserMedia` window.
   */
  let startSeq = 0
  let gen = 0
  let capTimer: ReturnType<typeof setTimeout> | null = null

  const writeOwner = (next: Owner | null): void => {
    owner = next
    deps.onOwnerChange?.(next)
    if (next === null && deps.disownPendingOnRelinquish) {
      startSeq++
      startPending = false
    }
  }

  /**
   * Startup SUCCEEDED — and is the LAST line of defence for a stuck mic, so it
   * decides from the state that exists NOW rather than from the intent it was
   * opened with:
   *
   *   - a LATCHED owner is meant to outlive its gesture, so an idle phase is
   *     expected. Leave it running.
   *   - a gesture owner with a non-idle phase — the press is still down
   *     (arming or holding). Leave it running; the release path owns the
   *     ending.
   *   - anything else — the owner was cleared by a teardown, or the gesture
   *     ended while startup was still in flight (no release coming, cap timer
   *     cleared) — is an ORPHAN. Stop it.
   *
   * The liveness test is the PHASE, not the generation: teardown bumps the
   * generation, so a generation comparison here is always false by the time a
   * released hold resolves; it reads like a guard and is dead code. And it is
   * scoped to `seq` so it only ever stops the session this call opened.
   */
  const settleStart = (seq: number): void => {
    if (startSeq !== seq) return
    startPending = false
    const current = owner
    if (current !== null && deps.isLatched?.(current)) return
    if (current !== null && deps.phase() !== 'idle') return
    writeOwner(null)
    deps.voice().stop()
  }

  /**
   * Startup FAILED. Nothing to commit, and a rejection can arrive with
   * resources already half-acquired: `useStreamingStt` builds its
   * `AudioContext` and worklet AFTER `getUserMedia` and the socket handshake,
   * outside any `try`, and `useVoiceInput`'s streaming branch re-raises rather
   * than catching. So a throw there leaves the mic stream open with no session
   * to stop — `cancel()` is what tears it down. Scoped to `seq`, so a
   * superseded startup's rejection cannot tear down the session that replaced
   * it.
   */
  const failStart = (seq: number): void => {
    if (startSeq !== seq) return
    startPending = false
    const current = owner
    writeOwner(null)
    deps.resetToIdle()
    if (current !== null) deps.voice().cancel()
  }

  return {
    launch(next: Owner): void {
      writeOwner(next)
      startPending = true
      const seq = ++startSeq
      const started = deps.voice().start()
      if (started && typeof (started as Promise<void>).then === 'function') {
        void (started as Promise<void>).then(
          () => { settleStart(seq) },
          () => { failStart(seq) },
        )
      } else {
        // Synchronous control (or one returning nothing): no startup window to
        // guard, so leave the owner in place and nothing pending.
        startPending = false
      }
    },

    beginHold(): void {
      const g = ++gen
      deps.setPhase('holding')
      // Hard ceiling: a release we never hear about must not hold the mic
      // forever. Both transports clear the previous timer before re-entering
      // `arming`, but this is a shared seam now — replace rather than leak a
      // prior handle so the invariant holds for a transport that does not.
      if (capTimer) clearTimeout(capTimer)
      capTimer = setTimeout(() => {
        capTimer = null
        if (gen === g && deps.phase() === 'holding') deps.disarm(true)
      }, MAX_HOLD_MS)
    },

    owner: (): Owner | null => owner,
    setOwner: writeOwner,
    startPending: (): boolean => startPending,
    generation: (): number => gen,
    bumpGeneration: (): number => ++gen,

    clearCapTimer(): void {
      if (capTimer) { clearTimeout(capTimer); capTimer = null }
    },
  }
}
