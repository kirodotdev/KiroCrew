/**
 * Push-to-talk / tap-to-toggle keyboard driver for voice input.
 *
 * Owns ONLY the key state machine; capture itself belongs to `useVoiceInput`,
 * which is injected as {@link VoiceControls} so this hook is testable without a
 * microphone.
 *
 * ```
 *  IDLE ──keydown(match)──▶ ARMING ──holdMs elapses──▶ HOLDING
 *    ▲                        │                          │
 *    │                     keyup (tap)                 keyup / watchdog
 *    └────────────────────────┴──────────────────────────┘
 * ```
 *
 * Two things make the ARMING state load-bearing rather than a nuisance delay:
 *
 * 1. **It disambiguates a tap from a hold** — the whole point of hybrid mode.
 * 2. **Capture is already running during it.** `start()` is called on the
 *    KEYDOWN, not when the threshold passes, so the word the user starts on is
 *    in the recording rather than clipped off the front of it. `getUserMedia`
 *    plus the first audio frame costs 50-200ms on macOS, and streaming pays a
 *    ~2-3s Transcribe handshake on top; waiting out the threshold first put all
 *    of that in front of the opening syllable (Whisper then hallucinates the
 *    silence into a canned phrase).
 *
 * ONE session serves the whole gesture. It is opened before anyone knows what
 * the press will turn out to be, so ownership — not intent-at-open — decides its
 * fate (`ownerRef`):
 *
 * | the press turns out to be      | what happens to that session      |
 * |--------------------------------|-----------------------------------|
 * | a hold (crosses the threshold) | becomes the hold, committed on release |
 * | a tap, hybrid mode             | becomes the latch, keeps running  |
 * | a release, toggle mode         | becomes the latch, keeps running  |
 * | a tap, hold-only mode          | discarded — a tap means nothing   |
 * | a chord (`⌥e` → é)             | discarded while arming            |
 *
 * Nothing is transmitted for a discarded press: `useStreamingStt` buffers PCM
 * locally until the server's `ready` frame, which lands well after the threshold
 * has already resolved the gesture, so `cancel()` drops the buffer unsent.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  loadPttConfig,
  matchesBinding,
  MAX_HOLD_MS,
  PTT_CHANGED_EVENT,
  type PttConfig,
  isBareModifier,
  stillHeld,
} from '../lib/pushToTalk'

/** The slice of `useVoiceInput` this hook drives. */
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

type Phase = 'idle' | 'arming' | 'holding'

export interface UsePushToTalkOpts {
  /** Disable entirely (e.g. STT off, or a modal owns the keyboard). */
  disabled?: boolean
}

/**
 * True when the keystroke came from inside an embedded terminal, where the key
 * belongs to the PTY. Mirrors `useKeyboardShortcuts.isTerminalTarget`.
 */
function isTerminalTarget(target: EventTarget | null): boolean {
  const el = target as Element | null
  return !!el && typeof el.closest === 'function' && !!el.closest('.xterm')
}

export function usePushToTalk(voice: VoiceControls, { disabled }: UsePushToTalkOpts = {}) {
  const [cfg, setCfg] = useState<PttConfig>(() => loadPttConfig())
  // Mirrored so the keydown handler reads the CURRENT phase without being
  // re-created (and re-bound) on every transition.
  const phaseRef = useRef<Phase>('idle')
  const [phase, setPhaseState] = useState<Phase>('idle')
  const setPhase = useCallback((p: Phase) => { phaseRef.current = p; setPhaseState(p) }, [])

  const armTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const capTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  /**
   * The in-flight `start()`: what it was opened FOR, or null when none is
   * pending. Set from just before the call until its promise settles.
   *
   *   `'hold'`  — a hold whose release must end the session.
   *   `'latch'` — a deliberate toggle / tap-latch that must OUTLIVE the keypress.
   *
   * Load-bearing for the stuck-mic guarantee, because a startup can fail to
   * settle AT ALL: the streaming path awaits a `ready` frame from the backend,
   * and a socket that opens and then goes silent leaves that await pending
   * forever. Any cleanup that runs only in the promise's own `.then()` therefore
   * inherits its liveness, and the hard cap — the one mechanism that does not —
   * has already been cleared by the time the key comes up. So every teardown in
   * this window runs SYNCHRONOUSLY off this ref instead of awaiting anything.
   *
   * The KIND is what two decisions turn on, neither of which can read
   * `voice.recording` (false for the whole `getUserMedia` + handshake window):
   *
   *   1. The settle handler telling an ORPHAN (a hold whose key is already up)
   *      from a LATCH the user wants left running.
   *   2. A second press cancelling a startup that has not gone live yet. Testing
   *      `recording` alone let that press fall through and open a SECOND
   *      `start()`, which `useVoiceInput`'s re-entrancy guard swallowed — so the
   *      FIRST startup still went live and the mic stayed on with the user
   *      believing they had just switched it off.
   *
   * Whether teardown commits or discards depends on the transport, not the kind:
   * streaming has a live socket buffering real PCM, batch has no recorder yet.
   * `cancel()` is the only call that aborts a batch startup — it releases the
   * warm mic so `acquireWarm` rejects instead of handing back a stream — and it
   * is also what releases the stream when startup REJECTS, since
   * `useStreamingStt` builds its `AudioContext` and worklet after the handshake
   * outside any `try` and `useVoiceInput` re-raises rather than catching.
   */
  /**
   * Who owns the open session right now, or null when nobody does.
   *
   *   `'gesture'` — the key press that opened it. Owns it while `phase` is
   *                 `arming` or `holding`; once `phase` returns to `idle` with
   *                 this owner still set, the session is an ORPHAN.
   *   `'latch'`   — a deliberate tap-latch or toggle. Outlives the keypress, so
   *                 an idle phase is expected and must not stop it.
   *
   * The session is opened on keydown, BEFORE anyone knows whether the press is a
   * tap, a hold, or a chord — so intent cannot be recorded at open time the way
   * it could when `start()` waited for the threshold. Ownership is written when
   * the gesture RESOLVES, and every teardown clears it, which is what lets the
   * settle handler tell "still wanted" from "nothing is holding this any more".
   */
  const ownerRef = useRef<'gesture' | 'latch' | null>(null)
  /**
   * True while `start()`'s async startup is in flight.
   *
   * Load-bearing for the stuck-mic guarantee, because a startup can fail to
   * settle AT ALL: the streaming path awaits a `ready` frame from the backend,
   * and a socket that opens and then goes silent leaves that await pending
   * forever. Any cleanup that runs only in the promise's own `.then()` therefore
   * inherits its liveness, and the hard cap — the one mechanism that does not —
   * may already have been cleared. So every teardown in this window runs
   * SYNCHRONOUSLY off this ref instead of awaiting anything.
   *
   * It is also the only way to know a session exists at all before it goes live:
   * `voice.recording` stays false for the whole `getUserMedia` + handshake
   * window, so a second press that tested only `recording` fell through and
   * opened a SECOND `start()` — which `useVoiceInput`'s re-entrancy guard
   * swallowed, leaving the first startup to go live against a user who had just
   * pressed to switch it off.
   *
   * Whether teardown commits or discards turns on `voice.recording` — "has
   * capture actually begun" — NOT on which transport is in use. `useStreamingStt`
   * flips recording true at the exact moment it has wired the worklet and PCM is
   * buffering, and only THEN awaits the server's `ready` frame, so:
   *
   *   - `recording` true  → real audio exists (buffered, pre-`ready`). `stop()`
   *     commits it; it sends the stop frame and arms its own force-cleanup.
   *   - `recording` false → still inside `getUserMedia`/permission. Nothing was
   *     captured and there is no socket, so `stop()` is a NO-OP and the startup
   *     would run to completion and go live after the release. Only `cancel()`
   *     aborts it.
   *
   * Keying this on `streamEnabled` instead was too coarse: it committed on the
   * streaming transport even when the release beat the permission grant, which
   * let a session open — and transmit post-release audio — for a press the user
   * had already finished. `cancel()` is also what releases a half-acquired stream
   * when startup REJECTS, since `useStreamingStt` builds its `AudioContext` and
   * worklet after the handshake outside any `try` and `useVoiceInput` re-raises
   * rather than catching.
   */
  const startPendingRef = useRef(false)
  /**
   * Monotonic per-`start()` sequence, bumped at EVERY call site AND by any
   * teardown that supersedes a pending startup.
   *
   * Lets a late-resolving startup tell "the session I opened" from "a session
   * someone else opened after me". Without it the settle handler's phase test is
   * an unconditional "not mine" and stops whatever is live — so a user who
   * releases and then immediately taps to latch (or clicks the mic button)
   * inside the `getUserMedia` window gets their new session killed by the old
   * hold's resolution. `useVoiceInput`'s own re-entrancy guard swallows that
   * second `start()`, so the FIRST promise is the one that actually goes live,
   * and leaving it running is what the user asked for.
   */
  const startSeqRef = useRef(0)
  /**
   * Bumped on every arm AND by `disarm`, so a timer armed by an earlier hold
   * cannot fire against a later one. Deliberately NOT the guard for the async
   * `start()` resolution -- `disarm` bumping it is exactly what made that guard
   * dead; see `beginHold`.
   */
  const genRef = useRef(0)
  // Live refs for the voice controls: the document-level listeners are bound
  // once, so reading through refs avoids re-binding them whenever the parent
  // re-renders and hands over new callback identities.
  const voiceRef = useRef(voice)
  voiceRef.current = voice
  const cfgRef = useRef(cfg)
  cfgRef.current = cfg
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled

  useEffect(() => {
    const onChange = () => setCfg(loadPttConfig())
    window.addEventListener(PTT_CHANGED_EVENT, onChange)
    // 'storage' fires for OTHER tabs/windows, so a rebind in Settings reaches a
    // second dashboard window too.
    window.addEventListener('storage', onChange)
    return () => {
      window.removeEventListener(PTT_CHANGED_EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])

  const clearTimers = useCallback(() => {
    if (armTimerRef.current) { clearTimeout(armTimerRef.current); armTimerRef.current = null }
    if (capTimerRef.current) { clearTimeout(capTimerRef.current); capTimerRef.current = null }
  }, [])

  /** Leave any armed/holding state, committing (`stop`) or discarding as told. */
  const disarm = useCallback((commit: boolean) => {
    const was = phaseRef.current
    clearTimers()
    genRef.current++
    setPhase('idle')
    if (was === 'holding') {
      // Startup still in flight. What that means depends on the path:
      //
      //   - STREAMING has already connected its worklet and is buffering PCM
      //     while it waits for the server's `ready` frame, so the user's speech
      //     is really in there. Commit it — `streamStop()` defers the stop frame
      //     until that buffer has been flushed, so the Transcribe stream ends
      //     AFTER the audio rather than before it, and it keeps a bounded
      //     force-cleanup either way, so the stuck-mic ceiling still holds.
      //   - BATCH has no recorder yet, so nothing was captured; `stop()` would
      //     do nothing at all, and only `cancel()` actually aborts the startup.
      if (startPendingRef.current) {
        // Clear the OWNER but leave the sequence alone: the settle handler is the
        // backstop for a startup that ignores this teardown and goes live
        // anyway, and bumping the sequence would make it read as stale and skip.
        ownerRef.current = null
        if (voiceRef.current.recording) voiceRef.current.stop()
        else voiceRef.current.cancel()
      } else {
        ownerRef.current = null
        // `recording`, not `startPending`, is the boundary that decides whether
        // `stop()` can reach anything — and this branch can be entered with a
        // startup STILL IN FLIGHT that we do not own. A mic-button start leaves
        // OUR `startPending` false, so a press during its acquisition window
        // falls through the second-press guard and calls `start()` again, which
        // the producer's re-entrancy latch swallows: it returns nothing, so
        // `launch` reads it as a synchronous control and clears `startPending`.
        // Releasing then took this branch and called `stop()` on a session whose
        // capture had not begun — a no-op — and the original startup went live
        // afterwards with the phase already back to idle and no owner watching
        // it. `cancel()` is the only call that aborts a pre-capture startup.
        if (commit && voiceRef.current.recording) voiceRef.current.stop()
        else voiceRef.current.cancel()
      }
    } else if (was === 'arming') {
      // The press never became a recording, so DISCARD — a sub-threshold tap in
      // hold-only mode, or a chord. Capture has been running since keydown, but
      // `useStreamingStt` is still buffering locally (its `ready` frame lands
      // seconds later), so `cancel()` drops that buffer unsent instead of
      // shipping half a keystroke to the transcriber. Batch has no recorder yet,
      // and `cancel()` aborts its acquisition either way.
      ownerRef.current = null
      voiceRef.current.cancel()
    }
  }, [clearTimers, setPhase])

  /**
   * Startup SUCCEEDED — and is the LAST line of defence for a stuck mic, so it
   * decides from the state that exists NOW rather than from the intent it was
   * opened with:
   *
   *   - owner `'latch'` — a tap-latch or toggle. Meant to outlive the keypress,
   *     so an idle phase is expected. Leave it running.
   *   - owner `'gesture'` with a non-idle phase — the key is still down (arming
   *     or holding). Leave it running; the release path owns the ending.
   *   - anything else — the owner was cleared by a teardown, or the gesture ended
   *     while startup was still in flight (no keyup coming, cap timer cleared) —
   *     is an ORPHAN. Stop it.
   *
   * Every teardown in the startup window clears `ownerRef` and deliberately does
   * NOT touch `seq`, so this handler still runs and can catch a startup that
   * ignored that teardown and went live regardless.
   *
   * The liveness test is the PHASE, not the generation: `disarm` bumps `genRef`,
   * so a generation comparison here is always false by the time a released hold
   * resolves; it reads like a guard and is dead code. And it is scoped to `seq`
   * so it only ever stops the session this call opened.
   */
  const settleStart = useCallback((seq: number) => {
    if (startSeqRef.current !== seq) return
    startPendingRef.current = false
    const owner = ownerRef.current
    if (owner === 'latch') return
    if (owner === 'gesture' && phaseRef.current !== 'idle') return
    ownerRef.current = null
    voiceRef.current.stop()
  }, [])

  /**
   * Startup FAILED. Nothing to commit, and a rejection can arrive with resources
   * already half-acquired: `useStreamingStt` builds its `AudioContext` and worklet
   * AFTER `getUserMedia` and the socket handshake, outside any `try`, and
   * `useVoiceInput`'s streaming branch re-raises rather than catching. So a throw
   * there leaves the mic stream open with no session to stop — `cancel()` is what
   * tears it down.
   *
   * Scoped to `seq`, so a superseded startup's rejection cannot tear down the
   * session that replaced it. Resets the phase directly rather than through
   * `disarm`: with no session left there is nothing to commit on a later keyup,
   * and leaving `phase` at `arming`/`holding` would let the release path try.
   */
  const failStart = useCallback((seq: number) => {
    if (startSeqRef.current !== seq) return
    startPendingRef.current = false
    const owner = ownerRef.current
    ownerRef.current = null
    clearTimers()
    genRef.current++
    if (phaseRef.current !== 'idle') setPhase('idle')
    if (owner !== null) voiceRef.current.cancel()
  }, [clearTimers, setPhase])

  /**
   * Open a session for the press that just started, and track the pending startup
   * so both a late resolution and a second press can reach it. EVERY `start()` in
   * this hook goes through here — a call site that skipped it left no way to
   * reach its own startup.
   */
  const launch = useCallback((owner: 'gesture' | 'latch') => {
    ownerRef.current = owner
    startPendingRef.current = true
    const seq = ++startSeqRef.current
    const started = voiceRef.current.start()
    if (started && typeof (started as Promise<void>).then === 'function') {
      void (started as Promise<void>).then(
        () => { settleStart(seq) },
        () => { failStart(seq) },
      )
    } else {
      // Synchronous control (or one returning nothing): no startup window to
      // guard, so leave the owner in place and nothing pending.
      startPendingRef.current = false
    }
  }, [settleStart, failStart])

  /**
   * The threshold passed, so the press is a HOLD. Capture has been running since
   * keydown — this only relabels the phase and arms the ceiling. There is no
   * `start()` here: a second one would be swallowed by `useVoiceInput`'s
   * re-entrancy guard, and the session opened on keydown is the one that already
   * has the opening word in it.
   */
  const beginHold = useCallback(() => {
    const gen = ++genRef.current
    setPhase('holding')
    // Hard ceiling: a release we never hear about must not hold the mic forever.
    capTimerRef.current = setTimeout(() => {
      if (genRef.current === gen && phaseRef.current === 'holding') disarm(true)
    }, MAX_HOLD_MS)
  }, [disarm, setPhase])

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      if (disabledRef.current) return
      // Auto-repeat: a held key fires keydown ~30x/sec. Only the first is an arm.
      if (e.repeat) return
      const { binding, mode, holdMs } = cfgRef.current

      // Any non-matching keystroke is also our chance to reconcile.
      if (!matchesBinding(e, binding)) {
        const phase = phaseRef.current
        if (phase === 'idle') return
        // The bound modifier is no longer physically down, so we missed its
        // keyup — commit what was said.
        if (!stillHeld(e, binding)) { disarm(true); return }
        // It IS still down and another key joined it: the user is typing a CHORD
        // with the bound modifier, not dictating. On macOS that is how you type
        // half the special characters (⌥V, ⌥3, ⌥5), so without this a quick ⌥V
        // read as a tap and LATCHED recording on, and a slower one started a
        // hold. Discard while arming — nothing was captured, and this is also
        // what stops the release from counting as a tap. Commit while holding:
        // a real utterance survives an accidental keypress, and a chord held
        // barely past the threshold yields a blob too short to transcribe.
        disarm(phase === 'holding')
        return
      }
      if (isTerminalTarget(e.target)) return
      // A chord binding's primary key would type a character (Space) or scroll;
      // claim it. A bare modifier produces nothing, so leave it alone — calling
      // preventDefault on a lone modifier can suppress legitimate chords the
      // user goes on to type.
      if (!isBareModifier(binding)) e.preventDefault()

      // Already capturing (latched by an earlier tap, or started from the mic
      // button) OR a startup we opened is still in flight: this press ENDS it,
      // and does not arm a new hold. `recording` alone is not enough — it stays
      // false for the whole getUserMedia + handshake window, so a second press
      // there used to fall through and open a second `start()` that
      // `useVoiceInput`'s re-entrancy guard swallowed, leaving the first startup
      // to go live against a user who thought they had switched it off.
      // The pending test is `startPending` AND still OWNED: a startup that a
      // previous teardown already disowned is on its way out (its settle handler
      // will stop it), so a fresh press must be free to arm a new gesture rather
      // than "ending" a session nobody holds.
      if (phaseRef.current === 'idle'
          && (voiceRef.current.recording
              || (startPendingRef.current && ownerRef.current !== null))) {
        const pending = startPendingRef.current
        // Clear the owner so the settle handler stops the session if the startup
        // lands anyway, but leave the sequence alone so that handler still runs.
        ownerRef.current = null
        if (pending) {
          // Startup still in flight: commit only if capture has actually begun
          // (`recording`), otherwise `stop()` is a no-op and the startup would
          // go live after this press — only `cancel()` aborts it.
          if (voiceRef.current.recording) voiceRef.current.stop()
          else voiceRef.current.cancel()
        } else voiceRef.current.stop()
        return
      }
      if (phaseRef.current !== 'idle') return

      if (mode === 'toggle') {
        // ARM it, exactly like the other modes — do not latch here. The chord
        // reconciliation above keys off a NON-IDLE phase, so a toggle press that
        // latched at keydown (leaving the phase `idle`) was invisible to it, to
        // the keyup handler, and to the blur/visibility guards alike: pressing
        // the bound modifier and then another key (⌥ then E for `é`) turned the
        // microphone on and nothing in the gesture machinery could turn it off
        // again. Capture still opens on the keydown; only the OWNERSHIP is
        // deferred to the release, which is what makes the press revocable.
        //
        // No hold timer: toggle mode has no hold semantics (the settings panel
        // hides the cutoff row for it), so the press simply stays `arming` until
        // it is released, joined by another key, or the window loses focus.
        setPhase('arming')
        launch('gesture')
        return
      }
      setPhase('arming')
      // Open capture NOW, before the tap/hold question is settled, so the word
      // the user starts on lands in the recording instead of being clipped by
      // the threshold plus the mic (and, on streaming, the Transcribe
      // handshake). Whichever way the gesture resolves, THIS session is the one
      // that serves it — or gets discarded unsent.
      launch('gesture')
      armTimerRef.current = setTimeout(() => {
        armTimerRef.current = null
        if (phaseRef.current === 'arming') beginHold()
      }, holdMs)
    }

    const onKeyUp = (e: KeyboardEvent) => {
      const { binding, mode } = cfgRef.current
      if (e.code !== binding.code) return
      const was = phaseRef.current
      if (was === 'idle') return
      if (was === 'holding') {
        // A pending startup is torn down synchronously inside `disarm`; the
        // settle handler is the backstop if it goes live regardless.
        disarm(true)
        return
      }
      // Released before the threshold — a TAP.
      clearTimers()
      genRef.current++
      setPhase('idle')
      if (mode === 'hybrid' || mode === 'toggle') {
        // Latch on by ADOPTING the session this press already opened — no second
        // `start()`, and the audio from before the threshold (the word the user
        // opened with) is already in it. Ownership moves from the gesture to the
        // latch, which is what tells a late-resolving startup to leave it alone.
        // Toggle mode arrives here for every release, since it never arms a hold.
        ownerRef.current = 'latch'
      } else {
        // Pure push-to-talk: a tap means nothing, so discard. `cancel()` drops
        // the streaming buffer before its `ready` frame — nothing was sent.
        ownerRef.current = null
        voiceRef.current.cancel()
      }
    }

    // A release that never arrives is the defining failure of a hold binding.
    // Losing focus or visibility mid-hold is the common cause, so commit what
    // was said instead of leaving the mic open.
    const onBlur = () => { if (phaseRef.current !== 'idle') disarm(true) }
    const onVisibility = () => { if (document.hidden && phaseRef.current !== 'idle') disarm(true) }

    document.addEventListener('keydown', onKeyDown, true)
    document.addEventListener('keyup', onKeyUp, true)
    window.addEventListener('blur', onBlur)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      document.removeEventListener('keydown', onKeyDown, true)
      document.removeEventListener('keyup', onKeyUp, true)
      window.removeEventListener('blur', onBlur)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [beginHold, clearTimers, disarm, launch, setPhase])

  // Unmounting mid-hold would orphan the timers AND the session. The key-up
  // listener goes away with this effect, so after unmount nothing is left that
  // would ever stop the microphone: not the cap timer (cleared here), not blur,
  // not visibilitychange. A startup still in flight is the worst case — the
  // producer's own unmount cleanup runs BEFORE that promise resolves, so the
  // stream it then assigns is one nothing will tear down.
  //
  // Teardown is written out rather than delegated to `disarm` on purpose:
  // `disarm` calls setPhase, and this runs while the component is going away.
  useEffect(() => () => {
    clearTimers()
    const pending = startPendingRef.current
    const owner = ownerRef.current
    ownerRef.current = null
    if (pending) {
      // Startup still in flight: commit buffered audio only if capture began,
      // otherwise abort it — `stop()` cannot reach a socket that does not exist.
      if (voiceRef.current.recording) voiceRef.current.stop()
      else voiceRef.current.cancel()
    } else if (owner !== null) {
      // A settled session with an owner. Commit a hold or a latch — that audio is
      // real speech the user expects to keep — and discard a press still arming,
      // which never became a recording.
      if (phaseRef.current === 'arming') voiceRef.current.cancel()
      else voiceRef.current.stop()
    }
  }, [clearTimers])

  return { config: cfg, phase, holding: phase === 'holding' }
}
