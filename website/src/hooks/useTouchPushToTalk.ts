/**
 * Touch hold-to-talk driver — the pointer sibling of {@link usePushToTalk}.
 *
 * Owns ONLY the pointer state machine; capture belongs to `useVoiceInput`, which
 * is injected as {@link VoiceControls} so this hook is testable without a
 * microphone. The two hooks deliberately share that interface, `PttConfig`, and
 * `MAX_HOLD_MS`: a user who has set `holdMs` to 300ms for their keyboard binding
 * means it for their thumb too, and a second config surface would let the two
 * disagree.
 *
 * ```
 *  IDLE ──pointerdown──▶ ARMING ──holdMs elapses──▶ HOLDING
 *    ▲                     │                          │
 *    │                  pointerup (tap)        pointerup / cancel / cap
 *    └─────────────────────┴──────────────────────────┘
 * ```
 *
 * Everything load-bearing is shared with the keyboard hook through
 * {@link createPttSession} — the ownership machinery itself — and the reasons
 * are recorded there rather than repeated here: capture opens on the DOWN (not
 * at the threshold) so the opening word is in the recording; ONE session serves
 * the whole gesture and its owner decides its fate when the gesture RESOLVES;
 * `voice.recording` — not the transport, not `startPending` — is what decides
 * whether a teardown may commit.
 *
 * Three things are genuinely new to touch:
 *
 * 1. **A cancel zone.** Dragging up past {@link CANCEL_THRESHOLD_PX} arms a
 *    discard, and dragging back down disarms it again, so the gesture is
 *    reversible right up to the release. This is the WeChat/Doubao contract and
 *    the only reason a hold gesture is safe on a surface with no Esc key.
 * 2. **`pointercancel` is touch's keyup-that-never-arrives.** iOS fires it
 *    whenever the system takes the gesture (a call, the app switcher, an edge
 *    swipe that wins). It is a DISCARD, not a commit: the user did not choose to
 *    finish, so committing would send half an utterance they never released on.
 * 3. **Pointer capture.** Without `setPointerCapture` the moves stop arriving as
 *    soon as the finger leaves the button — which is exactly what the cancel
 *    gesture does — and the release lands on whatever element is under the
 *    finger instead of on us.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  loadPttConfig,
  PTT_CHANGED_EVENT,
  type PttConfig,
} from '../lib/pushToTalk'
import {
  createPttSession,
  type PttPhase,
  type PttSession,
  type VoiceControls,
} from '../lib/pttSession'

/**
 * Upward travel, in CSS px, that arms the discard.
 *
 * Sized against the thumb, not the design: the hold target sits in the composer
 * row and the cancel cue renders directly above it, ~46px away, so a threshold
 * much below this fires on the drift of simply holding still. 56px is a
 * deliberate dead zone on top of that gap — far enough that holding steady
 * never arms it, close enough to reach without repositioning the hand.
 */
export const CANCEL_THRESHOLD_PX = 56

type Phase = PttPhase

export interface UseTouchPushToTalkOpts {
  /**
   * The element the gesture binds to, or null when it is not mounted.
   *
   * Deliberately the ELEMENT and not a ref object. The hold target mounts LATER
   * than this hook — it appears only once the user switches into hold mode — and
   * a ref's identity never changes, so an effect keyed on the ref runs once
   * against `null`, returns early, and never rebinds when the element arrives.
   * Passing the element makes "the target changed" observable, which is the only
   * thing that can drive the listeners onto it.
   */
  target: HTMLElement | null
  /** Disable entirely (STT off, composer disabled, a modal owns the surface). */
  disabled?: boolean
}

/**
 * The hold bar's visible states. `settling` is the window after a release where
 * the gesture is already idle but capture has not finished draining — without a
 * name for it the bar fell back to its resting label over a live microphone.
 * `tap-too-short` is the transient acknowledgement of a discarded tap: capture
 * opened on the press, so something WAS dropped, and dropping it silently on the
 * most likely first gesture teaches nothing.
 */
export type HoldBarState = 'idle' | 'holding' | 'armed-cancel' | 'settling' | 'tap-too-short'

/** How long the discarded-tap cue stays up. Long enough to read, short enough
 *  that it cannot be mistaken for a state the next press has to clear. */
export const TAP_CUE_MS = 1600

export interface TouchPushToTalkState {
  phase: Phase
  /** True once the press has been recognised as a hold. */
  holding: boolean
  /** True while the finger is inside the cancel zone — release will DISCARD. */
  armedCancel: boolean
  /**
   * True while a live gesture owns the capture session: set at the pointerdown
   * that opens capture, relinquished when the gesture resolves — release,
   * cancel, failed start, abandon. Exported from the session core's owner
   * itself so the consumer never has to infer ownership from a mounted DOM
   * node or from a recording flag: both proxies also match captures opened
   * elsewhere (the mic button, the keyboard binding on a device that has both
   * inputs), which is the defect class this export closes (#5753).
   *
   * Deliberately NOT true through the post-release drain — a commit relinquishes
   * ownership at the release, and `bar === 'settling'` is the name for what
   * remains of that session.
   */
  owns: boolean
  /**
   * Every visible state of the hold bar, named by the state machine that owns
   * them. The consumer renders one label and one appearance per value and does
   * NOT reassemble them: reconstructing `settling` out here as
   * `recording && phase === 'idle'` was correct only for the states that existed
   * when it was written, so adding a state to this hook silently made the bar
   * describe the wrong one. A new state must appear here — and the consumer's
   * switch stops compiling until it is handled.
   *
   * Precedence is the bar's, not the machine's: `armed-cancel` outranks
   * `holding` because both are true mid-slide and the discard is the one the
   * user needs told. An `arming` press reads `idle` — it lasts under `holdMs`
   * and has not yet promised anything.
   */
  bar: HoldBarState
}

export function useTouchPushToTalk(
  voice: VoiceControls,
  { target, disabled }: UseTouchPushToTalkOpts,
): TouchPushToTalkState {
  const [cfg, setCfg] = useState<PttConfig>(() => loadPttConfig())
  const phaseRef = useRef<Phase>('idle')
  const [phase, setPhaseState] = useState<Phase>('idle')
  const setPhase = useCallback((p: Phase) => { phaseRef.current = p; setPhaseState(p) }, [])

  // Mirrored the same way as `phase`: the pointermove handler must read the
  // current value without being re-created on every arm/disarm.
  const armedRef = useRef(false)
  const [armedCancel, setArmedCancelState] = useState(false)
  const setArmedCancel = useCallback((v: boolean) => {
    if (armedRef.current === v) return
    armedRef.current = v
    setArmedCancelState(v)
  }, [])

  const armTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  /**
   * Render-visible mirror of the session core's owner, exported as `owns`.
   * Written ONLY through the core's `onOwnerChange` seam, so the core keeps
   * its sole-writer property and stays the synchronous authority the handlers
   * read; this is the same fact at render time. The core's owner alone cannot
   * be exported — reading it during render is unobservable, so a consumer's
   * predicate would not recompute when ownership changes. Mirrored the same
   * way `phase` is, and for the same reason.
   */
  const [owns, setOwns] = useState(false)
  /**
   * This gesture committed and its capture has not finished draining yet.
   *
   * Recorded rather than inferred. "Idle and something is recording" is a
   * DIFFERENT claim: on a coarse-pointer device with a hardware keyboard, the
   * keyboard binding starts a recording this hook never sees, and the bar cannot
   * tell that apart from its own drain — it announced one over live speech.
   * Cleared lazily, because `settling` is `draining && recording`: once capture
   * really ends the bar reads idle whatever this still says, and the next press
   * resets it.
   */
  const [draining, setDraining] = useState(false)
  /** The discarded-tap cue. Its own timer, cleared by the next press so the cue
   *  can never sit on top of a live gesture. */
  const [tapCue, setTapCue] = useState(false)
  const cueTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const clearTapCue = useCallback(() => {
    if (cueTimerRef.current) { clearTimeout(cueTimerRef.current); cueTimerRef.current = null }
    setTapCue(false)
  }, [])
  const showTapCue = useCallback(() => {
    if (cueTimerRef.current) clearTimeout(cueTimerRef.current)
    setTapCue(true)
    cueTimerRef.current = setTimeout(() => { cueTimerRef.current = null; setTapCue(false) }, TAP_CUE_MS)
  }, [])
  /** Y where the press began, in client coords — the origin the drag measures from. */
  const originYRef = useRef(0)
  /** The pointer this gesture owns. A second finger must not steer it. */
  const pointerIdRef = useRef<number | null>(null)

  const voiceRef = useRef(voice)
  voiceRef.current = voice
  const cfgRef = useRef(cfg)
  cfgRef.current = cfg
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled

  /**
   * Late-bound seams the session core calls back into. `disarm` and `toIdle`
   * are defined below in terms of the core, so they cannot be closed over at
   * construction time; the core reads whatever this ref holds at call time,
   * and every render re-points it at the current callbacks.
   */
  const coreSeamsRef = useRef({ resetToIdle: () => {}, disarm: (_commit: boolean) => {} })

  /**
   * The shared session-ownership core. Constructed once — it holds the owner,
   * the pending-startup flag, the startup sequence, the gesture generation and
   * the hard-cap timer, all of which must survive re-renders the same way a
   * ref does.
   *
   * The touch parameterization — the full argument for each policy lives on
   * {@link PttSessionDeps}, the single place it is kept current:
   *   - No `isLatched`: nothing here survives its own gesture. A tap does NOT
   *     latch a running session — `VoiceControls` reports neither a refused
   *     start nor a session end, so a latch invariant here has nothing sound
   *     to stand on; tap-to-latch returns when the controls can report those
   *     two facts.
   *   - `disownPendingOnRelinquish` is ON: a replacement session on touch can
   *     come from the mic button, a surface this hook never sees, so an
   *     abandoned startup must be invalidated by sequence.
   *   - `onOwnerChange` mirrors ownership into the render-visible `owns`.
   */
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
  }, [])

  const clearTimers = useCallback(() => {
    if (armTimerRef.current) { clearTimeout(armTimerRef.current); armTimerRef.current = null }
    session.clearCapTimer()
  }, [session])

  /**
   * Return the machine to idle, clearing EVERY piece of per-gesture state.
   *
   * A helper rather than four hand-written resets, because that is exactly what
   * the misses looked like: a failed startup once cleared the phase and the
   * cancel flag but left `pointerIdRef` set, and since `onPointerDown` refuses
   * a press while that ref is non-null, ONE rejected streaming `start()` killed
   * the hold target outright until the composer remounted. Ownership is
   * deliberately NOT reset here: a startup can still be in flight when the
   * phase resets, and only the call site knows whether that startup should be
   * disowned, so it stays where the intent is visible.
   */
  const toIdle = useCallback(() => {
    clearTimers()
    session.bumpGeneration()
    setPhase('idle')
    setArmedCancel(false)
    pointerIdRef.current = null
  }, [clearTimers, session, setPhase, setArmedCancel])
  coreSeamsRef.current.resetToIdle = toIdle

  /**
   * Leave any armed/holding state, committing (`stop`) or discarding as told.
   *
   * `commit` is the CALLER's intent; `voice.recording` is the veto. A release
   * that beats the permission grant has nothing to commit, and `stop()` on a
   * session whose capture never began is a no-op that leaves the startup to go
   * live afterwards with nobody watching it — so only `cancel()` can end that
   * window. See `usePushToTalk.disarm` for the full argument.
   */
  const disarm = useCallback((commit: boolean) => {
    const was = phaseRef.current
    toIdle()
    if (was === 'idle') return
    session.setOwner(null)
    // `arming` never commits: the press never became a hold, so there is at most
    // a locally-buffered fragment and `cancel()` drops it unsent.
    if (commit && was === 'holding' && voiceRef.current.recording) {
      /*
       * Only THIS path can produce a drain worth naming. `settling` used to be
       * inferred as "idle and something is recording", which is not the same
       * claim: on a coarse-pointer device with a hardware keyboard, the keyboard
       * binding can start a recording while this gesture sits idle, and the bar
       * then announced a drain over speech that was actively being captured.
       * A discard sets nothing — there is no drain to describe once the audio has
       * been thrown away.
       */
      setDraining(true)
      voiceRef.current.stop()
    } else voiceRef.current.cancel()
  }, [toIdle, session])
  coreSeamsRef.current.disarm = disarm

  /**
   * Give up whatever this gesture owns because its TARGET is going away — hold
   * mode switched off, the composer was replaced by another surface, the hook
   * unmounted.
   *
   * The invariant: a gesture may not outlive the element it is bound to. Removing
   * the listeners without enforcing it left a press in flight with no release
   * ever arriving, and `MAX_HOLD_MS` later the hard cap COMMITTED it — the mic
   * stayed open for up to two minutes and then transcribed audio the user never
   * released on into a composer they had already navigated away from.
   *
   * A press in flight is DISCARDED, for the same reason `pointercancel` is: the
   * user never chose to finish, so there is no choice to honour.
   *
   * This is the ONLY teardown path — a gesture can only exist once a target has
   * existed, so every way one can end runs through here.
   */
  const abandon = useCallback(() => {
    if (phaseRef.current !== 'idle') disarm(false)
  }, [disarm])

  useEffect(() => {
    const el = target
    if (!el) return

    const onPointerDown = (e: PointerEvent) => {
      if (disabledRef.current) return
      // Multi-touch: the first finger owns the gesture, later ones are noise.
      if (!e.isPrimary || pointerIdRef.current !== null) return

      // Capture is already live, or a startup this gesture owns is still in
      // flight. Nothing here survives its own gesture any more, so in practice
      // this is a session opened elsewhere — the mic button, or the keyboard
      // binding on a device that has both. One microphone at a time: the press
      // ends what is running rather than opening a second `start()`, which
      // `useVoiceInput`'s re-entrancy guard would swallow while the first startup
      // went live anyway, against a user who believed they had just stopped it.
      // Testing `recording` alone let a press inside the acquisition window fall
      // through, which is why the pending term is here.
      if (voiceRef.current.recording || (session.startPending() && session.owner() !== null)) {
        toIdle()
        session.setOwner(null)
        if (voiceRef.current.recording) voiceRef.current.stop()
        else voiceRef.current.cancel()
        return
      }

      // Suppress the long-press text-selection callout and the synthetic
      // mouse/click pair. Without this iOS opens its selection loupe mid-hold and
      // swallows the moves the cancel gesture is measured from.
      e.preventDefault()
      clearTapCue()
      setDraining(false)
      pointerIdRef.current = e.pointerId
      originYRef.current = e.clientY
      // Keeps moves and the release coming to US after the finger leaves the
      // button — which is precisely what dragging into the cancel zone does.
      if (typeof el.setPointerCapture === 'function') {
        try { el.setPointerCapture(e.pointerId) } catch { /* not captured; window listeners still cover it */ }
      }
      setArmedCancel(false)
      setPhase('arming')
      session.launch('gesture')
      const gen = session.generation()
      armTimerRef.current = setTimeout(() => {
        if (session.generation() === gen && phaseRef.current === 'arming') session.beginHold()
      }, cfgRef.current.holdMs)
    }

    const onPointerMove = (e: PointerEvent) => {
      if (pointerIdRef.current !== e.pointerId) return
      if (phaseRef.current === 'idle') return
      setArmedCancel(originYRef.current - e.clientY >= CANCEL_THRESHOLD_PX)
    }

    const onPointerUp = (e: PointerEvent) => {
      if (pointerIdRef.current !== e.pointerId) return
      const phase = phaseRef.current
      if (phase === 'idle') return
      /*
       * The RELEASE POSITION decides, not the armed mirror. A pointermove can be
       * coalesced into the pointerup that follows it, so a fast flick up and let
       * go delivers no move at the cancel distance at all: `armedRef` stays false
       * and the release transcribed audio the user had just thrown away. This
       * event carries where the finger actually left, which is the only reading
       * that cannot be dropped. `armedRef` remains what the bar renders — it can
       * lag the finger by a frame, but it no longer decides anything.
       */
      if (originYRef.current - e.clientY >= CANCEL_THRESHOLD_PX) { disarm(false); return }
      if (phase === 'holding') { disarm(true); return }
      /*
       * A sub-threshold tap DISCARDS. Capture opened on the pointerdown so the
       * opening word is never clipped, but a press that never became a hold is
       * not a recording the user asked for, and the fragment goes unsent.
       *
       * A tap does NOT latch a running session — see the session construction
       * comment above for why tap-to-latch is deliberately absent here.
       *
       * The discard is ACKNOWLEDGED rather than silent: capture opened on the
       * press, so a fragment really was dropped, and the most likely first gesture
       * on a new control is a tap. Saying nothing there teaches nothing.
       */
      disarm(false)
      showTapCue()
    }

    const onPointerCancel = (e: PointerEvent) => {
      if (pointerIdRef.current !== e.pointerId) return
      // The system took the gesture. The user never released, so there is no
      // choice to honour — discard rather than send something they were still
      // in the middle of.
      disarm(false)
    }

    const onVisibility = () => {
      if (document.visibilityState !== 'hidden') return
      // Backgrounded mid-gesture. A hold has real audio and the user was
      // speaking, so commit it; an arming press has nothing worth keeping.
      // Nothing survives its own gesture, so an idle machine owns no microphone.
      disarm(phaseRef.current === 'holding')
    }

    el.addEventListener('pointerdown', onPointerDown)
    el.addEventListener('pointermove', onPointerMove)
    el.addEventListener('pointerup', onPointerUp)
    el.addEventListener('pointercancel', onPointerCancel)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      el.removeEventListener('pointerdown', onPointerDown)
      el.removeEventListener('pointermove', onPointerMove)
      el.removeEventListener('pointerup', onPointerUp)
      el.removeEventListener('pointercancel', onPointerCancel)
      document.removeEventListener('visibilitychange', onVisibility)
      // Listeners first, THEN the teardown: nothing may re-enter the state
      // machine while it is being dismantled.
      abandon()
      // A cue for a bar that is going away says nothing, and its timer would
      // outlive the hook.
      clearTapCue()
    }
    // Rebinds whenever the target appears, changes, or unmounts — which is the
    // whole reason this takes an element rather than a ref. Everything else is
    // read through refs, so a parent re-render does not re-bind.
  }, [target, session, disarm, toIdle, abandon, setPhase, setArmedCancel, showTapCue, clearTapCue])

  /*
   * Close the drain the moment capture actually ends.
   *
   * Masking it was not enough. `draining && recording` reads `idle` once capture
   * stops, but the flag itself stayed set, and a LATER recording this gesture did
   * not start — the keyboard binding on a device that has both inputs — re-lit it
   * and put `Finishing…` over live speech. That is the very defect `draining`
   * exists to prevent, delayed by one capture rather than fixed.
   *
   * The flag is now bounded by two observable edges: set where this gesture calls
   * `stop()`, cleared here when the transport reports capture over. The pointerdown
   * reset stays as a belt for the case where a press arrives first.
   */
  useEffect(() => {
    if (!voice.recording) setDraining(false)
  }, [voice.recording])

  // `voice.recording` is the ungated "is capture in flight" flag, which is the
  // only thing that can distinguish a settling bar from a resting one.
  //
  // The tap cue sits BELOW every live state: a new press clears it outright, and
  // a drain that is still finishing is the more important thing to say.
  const bar: HoldBarState = armedCancel
    ? 'armed-cancel'
    : phase === 'holding'
      ? 'holding'
      : phase === 'idle' && draining && voice.recording
        ? 'settling'
        : tapCue
          ? 'tap-too-short'
          : 'idle'

  return { phase, holding: phase === 'holding', armedCancel, owns, bar }
}
