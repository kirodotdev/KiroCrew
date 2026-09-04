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
 * fate (the session core's `owner`):
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
 *
 * The ownership machinery itself — who owns the session, the in-flight-startup
 * flag, the startup sequence, the hard-cap timer — lives in
 * {@link createPttSession}, shared with the touch transport. This hook keeps
 * only what is keyboard: key/chord matching, the latch, and the reconciliation
 * paths a keyboard needs (a keyup that never arrives, a chord joining a held
 * modifier, blur and visibility loss).
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  loadPttConfig,
  matchesBinding,
  PTT_CHANGED_EVENT,
  type PttConfig,
  isBareModifier,
  stillHeld,
} from '../lib/pushToTalk'
import {
  createPttSession,
  type PttPhase,
  type PttSession,
  type VoiceControls,
} from '../lib/pttSession'

// The interface predates the shared core and every consumer (including both
// hook test suites) imports it from here, so it stays part of this module's
// public surface.
export type { VoiceControls }

type Phase = PttPhase

/**
 * The keyboard's owner kinds.
 *
 *   `'gesture'` — the key press that opened the session.
 *   `'latch'`   — a deliberate tap-latch or toggle. Outlives the keypress, so
 *                 an idle phase is expected and must not stop it — which is
 *                 exactly what the `isLatched` predicate below tells the core.
 */
type KeyOwner = 'gesture' | 'latch'

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
  // Live refs for the voice controls: the document-level listeners are bound
  // once, so reading through refs avoids re-binding them whenever the parent
  // re-renders and hands over new callback identities.
  const voiceRef = useRef(voice)
  voiceRef.current = voice
  const cfgRef = useRef(cfg)
  cfgRef.current = cfg
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled

  /**
   * Late-bound seams the session core calls back into. `disarm` and the reset
   * path are defined below in terms of the core, so they cannot be closed over
   * at construction time; the core reads whatever this ref holds at call time,
   * and every render re-points it at the current callbacks.
   */
  const coreSeamsRef = useRef({ resetToIdle: () => {}, disarm: (_commit: boolean) => {} })

  /**
   * The shared session-ownership core. Constructed once — it holds the owner,
   * the pending-startup flag, the startup sequence, the gesture generation and
   * the hard-cap timer, all of which must survive re-renders the same way a
   * ref does.
   *
   * The keyboard's parameterization:
   *   - `isLatched` names the owner kind that outlives its gesture, feeding
   *     the settle handler's latch branch.
   *   - `disownPendingOnRelinquish` stays OFF: every teardown here clears the
   *     owner but deliberately leaves the startup sequence alone, so a startup
   *     that ignores the teardown and goes live anyway is still caught and
   *     stopped by its settle handler.
   */
  const sessionRef = useRef<PttSession<KeyOwner> | null>(null)
  if (!sessionRef.current) {
    sessionRef.current = createPttSession<KeyOwner>({
      voice: () => voiceRef.current,
      phase: () => phaseRef.current,
      setPhase,
      resetToIdle: () => { coreSeamsRef.current.resetToIdle() },
      disarm: (commit) => { coreSeamsRef.current.disarm(commit) },
      isLatched: (owner) => owner === 'latch',
    })
  }
  const session = sessionRef.current

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
    session.clearCapTimer()
  }, [session])

  /**
   * The reset the core runs on a FAILED startup: timers, generation, phase —
   * the keyboard has no other per-gesture state to clear.
   */
  const resetToIdle = useCallback(() => {
    clearTimers()
    session.bumpGeneration()
    if (phaseRef.current !== 'idle') setPhase('idle')
  }, [clearTimers, session, setPhase])
  coreSeamsRef.current.resetToIdle = resetToIdle

  /** Leave any armed/holding state, committing (`stop`) or discarding as told. */
  const disarm = useCallback((commit: boolean) => {
    const was = phaseRef.current
    clearTimers()
    session.bumpGeneration()
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
      if (session.startPending()) {
        // Clear the OWNER but leave the sequence alone: the settle handler is the
        // backstop for a startup that ignores this teardown and goes live
        // anyway, and bumping the sequence would make it read as stale and skip.
        session.setOwner(null)
        if (voiceRef.current.recording) voiceRef.current.stop()
        else voiceRef.current.cancel()
      } else {
        session.setOwner(null)
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
      session.setOwner(null)
      voiceRef.current.cancel()
    }
  }, [clearTimers, session, setPhase])
  coreSeamsRef.current.disarm = disarm

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
              || (session.startPending() && session.owner() !== null))) {
        const pending = session.startPending()
        // Clear the owner so the settle handler stops the session if the startup
        // lands anyway, but leave the sequence alone so that handler still runs.
        session.setOwner(null)
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
        session.launch('gesture')
        return
      }
      setPhase('arming')
      // Open capture NOW, before the tap/hold question is settled, so the word
      // the user starts on lands in the recording instead of being clipped by
      // the threshold plus the mic (and, on streaming, the Transcribe
      // handshake). Whichever way the gesture resolves, THIS session is the one
      // that serves it — or gets discarded unsent.
      session.launch('gesture')
      armTimerRef.current = setTimeout(() => {
        armTimerRef.current = null
        if (phaseRef.current === 'arming') session.beginHold()
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
      session.bumpGeneration()
      setPhase('idle')
      if (mode === 'hybrid' || mode === 'toggle') {
        // Latch on by ADOPTING the session this press already opened — no second
        // `start()`, and the audio from before the threshold (the word the user
        // opened with) is already in it. Ownership moves from the gesture to the
        // latch, which is what tells a late-resolving startup to leave it alone.
        // Toggle mode arrives here for every release, since it never arms a hold.
        session.setOwner('latch')
      } else {
        // Pure push-to-talk: a tap means nothing, so discard. `cancel()` drops
        // the streaming buffer before its `ready` frame — nothing was sent.
        session.setOwner(null)
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
  }, [clearTimers, disarm, session, setPhase])

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
    const pending = session.startPending()
    const owner = session.owner()
    session.setOwner(null)
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
  }, [clearTimers, session])

  return { config: cfg, phase, holding: phase === 'holding' }
}
