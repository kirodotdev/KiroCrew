/**
 * Pointer hold-to-talk on the message box — the mouse/trackpad sibling of
 * {@link useTouchPushToTalk}.
 *
 * Press and hold a mouse or trackpad button on the composer's text box for
 * `PttConfig.pointerHoldMs` and dictation starts; let go and it stops. Opt-in
 * through `PttConfig.pointerHold` (Settings → Voice), because the text box already has a
 * meaning for a press: it places the caret and drags select text. Both must keep
 * working with the gesture on, which shapes everything below:
 *
 * ```
 *  IDLE ──pointerdown──▶ ARMING ──pointerHoldMs elapses, no travel──▶ HOLDING
 *    ▲                     │                                     │
 *    │        pointerup (a click) / travel ≥ ARM_MOVE_PX   pointerup (commit)
 *    │                     │                           travel ≥ CANCEL_MOVE_PX
 *    │                     │                           pointercancel (discard)
 *    └─────────────────────┴─────────────────────────────────────┘
 * ```
 *
 * Where it departs from the touch hook, and why:
 *
 * 1. **Capture opens at the threshold, not on the press.** The touch hook opens
 *    the microphone on `pointerdown` so the first word is never clipped, and can
 *    afford to: its target exists only to be held. Here every caret placement
 *    is a `pointerdown`, and opening capture on each one would flash the
 *    system's microphone indicator and prompt for permission on a plain click.
 *    The recording starts when the press has proven itself a hold; the status
 *    bar or dictation panel appearing is the cue to speak.
 * 2. **Movement cancels, in both phases.** While arming, travel past
 *    {@link ARM_MOVE_PX} means a drag-select is under way, so the press is
 *    returned to the text box untouched. While holding, travel past
 *    {@link CANCEL_MOVE_PX} DISCARDS the recording at once — there is no armed
 *    zone to slide back out of, because a pointer has an Esc key and a mic
 *    button for the deliberate paths, and "I moved" is the one gesture that
 *    must never send.
 * 3. **`preventDefault` is never called** on the press. The browser's own
 *    handling of it IS the caret placement and the focus the transcript needs.
 * 4. **No pointer capture.** The moves and the release are read from
 *    `document`, so the gesture follows the pointer off the box without taking
 *    the events away from the text box's own selection handling.
 * 5. **A hold inserts at a caret, never over a range.** Points 3 and 4 leave
 *    the browser drag-selecting while the button is held, so from the cutoff to
 *    the release the selection is kept collapsed (see `holdCaret`).
 *
 * The session-ownership machinery is {@link createPttSession}, shared with the
 * other two transports and documented there.
 */
import { useCallback, useEffect, useRef, useState } from 'react'

import {
  loadPttConfig,
  type PttConfig,
} from '../lib/pushToTalk'
import {
  type PttPhase,
  type StopIntent,
  type VoiceControls,
} from '../lib/pttSession'
import { usePttGestureSession } from './usePttGestureSession'

/**
 * Travel, in CSS px, past which an ARMING press is a drag rather than a hold.
 *
 * Small on purpose: a hand resting still on a trackpad drifts a pixel or two,
 * and a drag-select moves far more than this in its first frame. Below it the
 * press stays a candidate hold; at or above it the text box gets its drag back
 * and the timer is dropped.
 */
export const ARM_MOVE_PX = 6

/**
 * Travel, in CSS px, past which a HOLDING press discards its recording.
 *
 * Wider than the arming band because a hand that has been holding still for
 * the cutoff and then speaks for a while drifts further than one that just
 * landed, and a discard throws away speech. Still well under the width of the
 * composer, so a deliberate slide reaches it without leaving the box.
 */
export const CANCEL_MOVE_PX = 24

/**
 * The element kinds a press must land in to arm. The composer's text column
 * also holds the staged quote and its remove button, which this gesture has
 * no business on; the plain textarea and the rich editor's `contenteditable`
 * root are the surfaces a press means "the message box".
 */
const EDITOR_SELECTOR = 'textarea,[contenteditable="true"]'

/**
 * Islands inside the rich editor that are not text: a paste-token chip is a
 * `contenteditable="false"` decorator with its own click and double-click, and
 * a control is a control. A press there operates that island, so it never arms.
 */
const NON_TEXT_SELECTOR = '[contenteditable="false"],button,[href],input,select,[role="button"]'

/** The message box a press landed in, or null when it landed elsewhere or on
 *  a non-text island inside the box. */
function editorTextTarget(target: EventTarget | null): Element | null {
  if (!(target instanceof Element)) return null
  const editor = target.closest(EDITOR_SELECTOR)
  if (!editor) return null
  const island = target.closest(NON_TEXT_SELECTOR)
  if (island && editor.contains(island) && island !== editor) return null
  return editor
}

/** A collapsed insertion point in the message box: a character offset in the
 *  plain textarea, a DOM position in the rich editor's `contenteditable`. */
type CaretPoint =
  | { kind: 'offset'; offset: number }
  | { kind: 'dom'; node: Node; offset: number }

/**
 * The editor's selection ANCHOR -- the end the press placed, as opposed to the
 * end a drag has since carried -- and whether the selection is collapsed. Null
 * when the document selection is not inside the editor.
 */
function selectionAnchor(editor: Element): { point: CaretPoint; collapsed: boolean } | null {
  if (editor instanceof HTMLTextAreaElement) {
    const { selectionStart: start, selectionEnd: end } = editor
    const offset = editor.selectionDirection === 'backward' ? end : start
    return { point: { kind: 'offset', offset }, collapsed: start === end }
  }
  const selection = editor.ownerDocument.getSelection()
  if (!selection?.anchorNode || !editor.contains(selection.anchorNode)) return null
  return {
    point: { kind: 'dom', node: selection.anchorNode, offset: selection.anchorOffset },
    collapsed: selection.isCollapsed,
  }
}

/** Collapse the editor's selection onto `point`, clamped to what the node now
 *  holds. False, with nothing changed, when the point's node has left the
 *  editor (the rich editor re-rendered that text). */
function collapseTo(editor: Element, point: CaretPoint): boolean {
  if (point.kind === 'offset') {
    if (!(editor instanceof HTMLTextAreaElement)) return false
    editor.setSelectionRange(point.offset, point.offset)
    return true
  }
  const { node } = point
  if (!node.isConnected || !editor.contains(node)) return false
  const size = node.nodeType === Node.TEXT_NODE ? (node.textContent?.length ?? 0) : node.childNodes.length
  editor.ownerDocument.getSelection()?.collapse(node, Math.min(point.offset, size))
  return true
}

type Phase = PttPhase

export interface UsePointerHoldToTalkOpts {
  /**
   * The element the gesture binds to, or null when it is not mounted. The
   * ELEMENT and not a ref object, for the reason `useTouchPushToTalk` gives:
   * only a changing element can re-run the binding effect.
   */
  target: HTMLElement | null
  /** Disable entirely (STT off, composer disabled or locked, a transcription
   *  or another composer holds the microphone). */
  disabled?: boolean
  /**
   * The hold's caret, told to the composer. `true` after the hold has collapsed
   * the selection (at the cutoff, after drift widened it again, and as the hold
   * ends): publish that caret as dictation's insertion point, and until `false`
   * take no range from the live selection. `false` once the hold is over, on
   * every exit. The composer's own caret recording cannot stand in for the
   * publish, because the browser holds back `select` events while the button
   * is down.
   */
  onCaretHeld?: (holding: boolean) => void
}

export interface PointerHoldToTalkState {
  phase: Phase
  /** True once the press has been recognised as a hold and capture requested. */
  holding: boolean
  /** True while a live gesture owns the capture session; see
   *  `TouchPushToTalkState.owns` for why ownership is exported rather than
   *  inferred from the recording flag. */
  owns: boolean
}

export function usePointerHoldToTalk(
  voice: VoiceControls,
  { target, disabled, onCaretHeld }: UsePointerHoldToTalkOpts,
): PointerHoldToTalkState {
  const [cfg, setCfg] = useState<PttConfig>(() => loadPttConfig())
  const phaseRef = useRef<Phase>('idle')
  const [phase, setPhaseState] = useState<Phase>('idle')
  const setPhase = useCallback((p: Phase) => { phaseRef.current = p; setPhaseState(p) }, [])

  const armTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  /** Where the press began, in client coords — the origin travel is measured from. */
  const originXRef = useRef(0)
  const originYRef = useRef(0)
  /** The pointer this gesture owns. */
  const pointerIdRef = useRef<number | null>(null)
  const [owns, setOwns] = useState(false)
  /** True once this hold's own capture has been seen live. Reset with the
   *  gesture; see the effect that watches `voice.recording` below. */
  const sawLiveRef = useRef(false)

  const voiceRef = useRef(voice)
  voiceRef.current = voice
  const cfgRef = useRef(cfg)
  cfgRef.current = cfg
  const disabledRef = useRef(disabled)
  disabledRef.current = disabled
  const onCaretHeldRef = useRef(onCaretHeld)
  onCaretHeldRef.current = onCaretHeld
  /** The editor the press landed in. */
  const editorRef = useRef<Element | null>(null)
  /** Where a hold inserts: the collapsed caret it last saw in `editorRef`. */
  const caretPinRef = useRef<CaretPoint | null>(null)
  /** From the cutoff until the hold ends: the composer was told `true`. */
  const caretHeldRef = useRef(false)

  /**
   * Keep a hold's insertion point a collapsed caret.
   *
   * The press is never prevented and never captured, so the browser treats
   * the held button plus the drift `CANCEL_MOVE_PX` tolerates as a drag-select.
   * Dictation splices over a selected range (dictating over a selection
   * replaces it), so letting that range stand would delete the drifted-over
   * part of the draft. A collapsed caret, wherever it is, becomes the pin: the
   * press point at the cutoff, or the spot a landing transcript moved the caret
   * to. A range is collapsed back onto the pin (onto its own anchor when the
   * pin's text is gone) and republished; `publish` republishes even an
   * untouched caret.
   */
  const holdCaret = useCallback((publish: boolean) => {
    const editor = editorRef.current
    const now = editor ? selectionAnchor(editor) : null
    if (editor && now) {
      const pin = caretPinRef.current
      if (now.collapsed || !pin || !collapseTo(editor, pin)) {
        caretPinRef.current = now.point
        if (!now.collapsed) collapseTo(editor, now.point)
      }
    }
    if (publish || (now && !now.collapsed)) onCaretHeldRef.current?.(true)
  }, [])

  /** Late-bound seams the session core calls back into; see `useTouchPushToTalk`. */
  const coreSeamsRef = useRef({ resetToIdle: () => {}, disarm: (_commit: boolean) => {} })

  /**
   * The shared session core, parameterised as touch is: no latch (nothing here
   * survives its own gesture) and `disownPendingOnRelinquish` on, because a
   * replacement session can come from the mic button or the keyboard binding,
   * surfaces this hook never sees. {@link usePttGestureSession} builds it for
   * both gestures, with the config listener and `clearTimers`.
   */
  const { session, clearTimers } = usePttGestureSession({
    voiceRef, phaseRef, setPhase, coreSeamsRef, armTimerRef, setOwns, setCfg,
  })

  /** Return the machine to idle, clearing every piece of per-gesture state.
   *  Ownership is deliberately left to the call site, as in the touch hook. */
  const toIdle = useCallback(() => {
    // A hold ends here on every path. A stop on a stream that has delivered
    // nothing yet freezes the insertion point as it stands now, so the drift
    // since the last selection change is collapsed and published first.
    if (caretHeldRef.current) {
      holdCaret(true)
      caretHeldRef.current = false
      onCaretHeldRef.current?.(false)
    }
    clearTimers()
    session.bumpGeneration()
    setPhase('idle')
    pointerIdRef.current = null
    sawLiveRef.current = false
    editorRef.current = null
    caretPinRef.current = null
  }, [clearTimers, session, setPhase, holdCaret])
  coreSeamsRef.current.resetToIdle = toIdle

  /**
   * Leave a hold, committing (`stop`) or discarding as told. `commit` is the
   * caller's intent and `voice.recording` the veto: a release that beats the
   * permission grant has nothing to commit, and only `cancel()` can end a
   * startup still in flight (see `usePushToTalk.disarm`). Once another surface
   * has already ended the capture, neither operation is safe: streaming may be
   * draining its committed final, and `cancel()` would discard it.
   *
   * An `arming` press never reaches the microphone — capture opens only at the
   * threshold — so leaving that phase is `toIdle` alone, not a disarm.
   *
   * `intent` comes only from a release the person chose; the cap timer and a
   * hidden page commit without one, so they never send.
   */
  const disarm = useCallback((commit: boolean, intent?: StopIntent) => {
    const was = phaseRef.current
    // Snapshot before relinquishing ownership: this session invalidates its
    // pending-start flag when the owner clears.
    const startupPending = session.startPending()
    const recording = voiceRef.current.recording
    toIdle()
    if (was !== 'holding') return
    session.setOwner(null)
    // The key or mic button may have ended this capture while the pointer was
    // still down. Its committed stream can still be draining, so the pointer
    // only relinquishes the stale gesture; it must not stop or cancel again.
    if (!startupPending && !recording) return
    if (commit && recording) voiceRef.current.stop(intent)
    else voiceRef.current.cancel()
  }, [toIdle, session])
  coreSeamsRef.current.disarm = disarm

  /**
   * Let go of the hold as soon as its capture ends from somewhere else: Escape,
   * the keyboard key, the mic button. `disarm` above decides from the live
   * `voice.recording`, which is the shared microphone, not this gesture's
   * capture. If the hold stayed open, a recording the key started afterwards,
   * with the pointer still down, would read as this gesture's own, and the next
   * move would discard it or the release would finish it early. With nothing
   * recording and no startup in flight, `disarm` only relinquishes; it never
   * calls `stop` or `cancel`, so a final still draining is kept.
   *
   * Keyed on the transition (seen live, then not), so a hold whose startup is
   * still acquiring the microphone is left alone.
   */
  useEffect(() => {
    if (phase !== 'holding') return
    if (voice.recording) { sawLiveRef.current = true; return }
    if (sawLiveRef.current) disarm(false)
  }, [phase, voice.recording, disarm])

  /** The target is going away: a gesture may not outlive the element it is
   *  bound to, and a press in flight is discarded for the reason
   *  `pointercancel` is -- the user never chose to finish. */
  const abandon = useCallback(() => {
    if (phaseRef.current === 'holding') disarm(false)
    else if (phaseRef.current !== 'idle') toIdle()
  }, [disarm, toIdle])

  useEffect(() => {
    const el = target
    if (!el) return

    const travel = (e: PointerEvent) => Math.hypot(e.clientX - originXRef.current, e.clientY - originYRef.current)

    const onPointerDown = (e: PointerEvent) => {
      if (disabledRef.current || !cfgRef.current.pointerHold) return
      // Touch has its own hold mode (`useTouchPushToTalk`), with a dedicated
      // target and a cancel zone sized for a thumb; this gesture is for the
      // pointer classes that have none.
      if (e.pointerType === 'touch') return
      // The primary button, pressed plainly. A right or middle press opens a
      // menu or pastes, Shift extends the selection, and the other modifiers
      // belong to the OS and the keyboard binding.
      if (e.button !== 0 || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return
      if (!e.isPrimary || pointerIdRef.current !== null) return
      // Only a press on the message box's text arms; the quote card and its
      // button share the column but are not the box, and a paste-token chip
      // inside the rich editor has a click of its own.
      const editor = editorTextTarget(e.target)
      if (!editor) return
      // A recording is live (the mic button, the keyboard key, or a hold this
      // gesture just released that is still draining), or a startup this
      // gesture owns is still in flight: a press in the box stays a caret
      // placement. Unlike the touch bar, pressing here is not a deliberate
      // stop -- the user may simply be moving the caret so the next transcript
      // lands where they want it.
      if (voiceRef.current.recording || session.startPending()) return

      pointerIdRef.current = e.pointerId
      editorRef.current = editor
      originXRef.current = e.clientX
      originYRef.current = e.clientY
      setPhase('arming')
      const gen = session.generation()
      armTimerRef.current = setTimeout(() => {
        armTimerRef.current = null
        if (session.generation() !== gen || phaseRef.current !== 'arming') return
        // Re-check the start conditions the pointerdown checked: the cutoff is a
        // window in which the composer can turn read-only (a turn starts) or
        // disabled, or the microphone can be taken by another surface -- the
        // keyboard push-to-talk key, or another composer. `disabledRef` folds in
        // typingLocked/micBlocked/optimizing and is current at fire time;
        // `voice.recording` is the shared mic. Opening capture anyway would land
        // a transcript where it cannot go, or let this gesture's release stop a
        // recording the keyboard started. Hand the press back instead.
        if (disabledRef.current || voiceRef.current.recording) { toIdle(); return }
        // The press has proven itself a hold: open capture now, as one session
        // owned by this gesture, and start the hard cap.
        session.launch('gesture')
        // Only enter `holding` if the start was ACCEPTED. A refused start --
        // another startup on this composer is still in flight (the key pressed
        // inside the cutoff window, before its capture went live), another chat
        // holds the microphone, STT is not ready -- returns without a promise and
        // without recording, which `launch` cannot tell apart from a synchronous
        // start. Holding anyway would let this gesture's release stop or discard
        // a recording it never opened. Accepted means a startup is now pending,
        // or capture is already live.
        if (!session.startPending() && !voiceRef.current.recording) {
          session.setOwner(null)
          toIdle()
          return
        }
        // The drift the arming band allows may already have selected a few
        // characters; from here on the press point is where the words go.
        caretHeldRef.current = true
        holdCaret(true)
        session.beginHold()
      }, cfgRef.current.pointerHoldMs)
    }

    const onPointerMove = (e: PointerEvent) => {
      if (pointerIdRef.current !== e.pointerId) return
      const phase = phaseRef.current
      if (phase === 'idle') return
      const d = travel(e)
      // Arming: a drag-select has begun. Hand the press back to the text box.
      if (phase === 'arming') { if (d >= ARM_MOVE_PX) toIdle(); return }
      // Holding: the pointer left the spot it was held on. Discard at once --
      // nothing is transcribed, nothing is sent.
      if (d >= CANCEL_MOVE_PX) disarm(false)
    }

    const onPointerUp = (e: PointerEvent) => {
      if (pointerIdRef.current !== e.pointerId) return
      const phase = phaseRef.current
      if (phase === 'idle') return
      // Released before the cutoff: an ordinary click, and the browser has
      // already placed the caret. Nothing was recorded, so nothing to discard.
      if (phase === 'arming') { toIdle(); return }
      // Released at the spot it was held on: a deliberate finish. The release
      // position decides, not a flag set by the last move, for the reason the
      // touch hook gives -- a final move can be coalesced into the release.
      if (travel(e) < CANCEL_MOVE_PX) disarm(true, 'finish')
      else disarm(false)
    }

    const onPointerCancel = (e: PointerEvent) => {
      if (pointerIdRef.current !== e.pointerId) return
      // The system took the gesture; the user never released, so discard.
      disarm(false)
    }

    const onVisibility = () => {
      if (document.visibilityState !== 'hidden') return
      // Backgrounded mid-gesture. A hold has real audio and the user was
      // speaking, so commit it; an arming press has nothing to keep.
      if (phaseRef.current === 'holding') disarm(true)
      else toIdle()
    }

    // The release is read from `document`, not from a pointer-captured element,
    // so a pointer that leaves the browser entirely (another app, the OS taking
    // focus) can swallow the pointerup. Without this, a hold then sat open until
    // the 120s hard cap. `blur` is the window-level counterpart to the tab
    // `visibilitychange` above, and ends the gesture the same way: commit a hold
    // (no intent, so it never sends), drop an arming press. Idle is a no-op, so
    // the ordinary focus loss of clicking away after releasing costs nothing.
    const onBlur = () => {
      if (phaseRef.current === 'holding') disarm(true)
      else if (phaseRef.current !== 'idle') toIdle()
    }

    // Drift during a hold keeps extending the browser's drag-select. Read in the
    // capture phase so the range is collapsed before the rich editor's own
    // (non-capture) document listener copies it into its state; capture also
    // sees a textarea's change whether the browser fires it at the document or
    // at the textarea itself.
    const onSelectionChange = () => {
      if (phaseRef.current === 'holding') holdCaret(false)
    }

    el.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('selectionchange', onSelectionChange, true)
    // Moves and the release are read from the document: the gesture follows the
    // pointer off the box, and the box keeps its own selection handling intact.
    document.addEventListener('pointermove', onPointerMove)
    document.addEventListener('pointerup', onPointerUp)
    document.addEventListener('pointercancel', onPointerCancel)
    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('blur', onBlur)
    return () => {
      el.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('selectionchange', onSelectionChange, true)
      document.removeEventListener('pointermove', onPointerMove)
      document.removeEventListener('pointerup', onPointerUp)
      document.removeEventListener('pointercancel', onPointerCancel)
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('blur', onBlur)
      // Listeners first, THEN the teardown: nothing may re-enter the state
      // machine while it is being dismantled.
      abandon()
    }
  }, [target, session, disarm, toIdle, abandon, setPhase, holdCaret])

  return { phase, holding: phase === 'holding', owns }
}
