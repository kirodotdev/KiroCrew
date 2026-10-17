import { useCallback, useEffect, useLayoutEffect, useMemo, useState } from 'react'
import { useDictationPanelUsable } from '../VoiceDictationPanel'
import { useTouchPushToTalk } from '../../hooks/useTouchPushToTalk'
import { useAppSelector } from '../../store'
import { safeSetItem } from '../../utils/safeStorage'
import { isTouchDevice } from '../../utils/isTouchDevice'
import { i18nT } from '../../i18n/t'
import type { ComposerVoiceInputProps } from '../../chat-core/composer/Composer'
import type { ComposerControl } from '../composerControl'

/* Dictation as the composer sees it. The Composer root's Voice atom
   (`chat-core/composer/useComposerVoice.ts`) owns capture, the one-mic mutex
   and the transcript splice; the composer reads its slice and owns what is
   on screen: the caret the splice lands at, the dictation panel, Escape to
   discard, and on touch the hold-to-talk mode that swaps the textarea for a
   press-and-hold target. */

type VoiceInput = Partial<ComposerVoiceInputProps>
/**
 * Whether the composer is in hold-to-talk mode (`'1'`) — the WeChat-style swap
 * where the textarea is replaced by a hold target. Persisted because using voice
 * is a habit rather than a per-message choice: someone who dictates does it all
 * day, and resetting to the keyboard on every mount taxes exactly them.
 */
const VOICE_MODE_LS_KEY = 'mc-voice-mode'

/** Zero-arg stand-in for an absent voice control. Separate from
 *  `noopSelectDevice`, whose one parameter makes it unassignable to `() => void`. */
const noopVoiceControl = () => {}

/** The dictation bridge: the caret a transcript splices in at, the panel that
 *  replaces the textarea while recording, focus while a transcript lands, and
 *  Escape to discard. */
export function useDictationControls({ composerControl, value, autoFocusKey, anyPickerOpenRef, voiceCaretRef, voicePendingCaretRef, voiceDictationPanel, voiceRecording, voiceDrainCancellable, voiceError, voiceSampleRef, voiceTranscribing, onVoiceCancel, onVoiceToggle }: {
  composerControl: () => ComposerControl | null
  value: string
  autoFocusKey?: string | null
  /** Any of the composer's trigger pickers is open: they own Escape. */
  anyPickerOpenRef: React.RefObject<boolean>
  voiceCaretRef: VoiceInput['voiceCaretRef']
  voicePendingCaretRef: VoiceInput['voicePendingCaretRef']
  voiceDictationPanel: boolean
  voiceRecording: boolean
  /** A released streaming utterance is still queued behind the recogniser and
   *  can be called off. True exactly through the post-release drain, when
   *  `voiceRecording` is already false — the window this control exists for. */
  voiceDrainCancellable: boolean
  voiceError: VoiceInput['voiceError']
  voiceSampleRef: VoiceInput['voiceSampleRef']
  voiceTranscribing: boolean
  onVoiceCancel: VoiceInput['onVoiceCancel']
  onVoiceToggle: VoiceInput['onVoiceToggle']
}) {
  const publishLexicalSelection = useCallback((selection: { start: number; end: number }) => {
    if (voiceCaretRef) voiceCaretRef.current = selection
  }, [voiceCaretRef])
  // Publish the live caret so ChatPage's dictation handler can splice a
  // transcript in at the cursor instead of appending. Written on every caret
  // move (typing, click, selection); the value persists through blur (clicking
  // the mic button), which is exactly when a batch transcript needs it.
  const recordCaret = useCallback(() => {
    const selection = composerControl()?.getSelection()
    if (selection && voiceCaretRef) voiceCaretRef.current = selection
  }, [composerControl, voiceCaretRef])
  // Restore the caret after a dictation transcript lands in `value`. The update
  // arrives via the parent (onChange → ChatPage setInput → value prop), so the
  // parent can't set the DOM selection itself. rAF mirrors applyPickedToken:
  // wait for the controlled value to commit before moving the caret. Cheap on
  // ordinary edits — it no-ops unless a dictation splice armed a pending caret.
  useLayoutEffect(() => {
    const pendingRef = voicePendingCaretRef
    const pos = pendingRef?.current
    if (!pendingRef || pos == null) {
      // No dictation restore pending: keep voiceCaretRef in sync with the live
      // selection, but ONLY once it has been established by a real interaction.
      // Guard on an already-non-null ref so an untouched textarea holding an
      // existing draft doesn't publish offset 0 here (which would make the next
      // batch transcript prepend at 0 instead of using the append fallback that
      // a null ref provides).
      const control = composerControl()
      const selection = control?.getSelection()
      if (selection && voiceCaretRef && voiceCaretRef.current) voiceCaretRef.current = selection
      return
    }
    pendingRef.current = null
    const raf = requestAnimationFrame(() => {
      const control = composerControl()
      if (!control) return
      const p = Math.min(pos, value.length)
      control.setSelection(p, p)
      if (voiceCaretRef) voiceCaretRef.current = { start: p, end: p }
    })
    // Cancel the frame if the slot switches (autoFocusKey) or value changes
    // again before it fires — otherwise the callback would stamp this slot's
    // caret onto whatever composer is mounted next.
    return () => cancelAnimationFrame(raf)
  }, [value, voicePendingCaretRef, voiceCaretRef, autoFocusKey, composerControl])
  // Dictation-panel gate. Three independent conditions must hold: the setting
  // is on, the browser has WebGL2, and the OS is not asking for reduced motion
  // (the hook covers the latter two). A mic error always falls through to
  // VoiceStatusBar, which owns the dismissible error affordance — the panel
  // has no way to surface it. Resolves to the sample ref (not a boolean) so
  // the non-optional prop narrows without a cast.
  const dictationUsable = useDictationPanelUsable(voiceDictationPanel)
  const showDictation =
    dictationUsable && voiceRecording && !voiceError && voiceSampleRef ? voiceSampleRef : null
  // Focus the composer when the dictation panel is up (as before) OR while a
  // batch transcript is landing (voiceTranscribing), so Enter sends and typing
  // edits the result. Deliberately NOT keyed on bare voiceRecording: focusing
  // during a STREAMING recording would invite mid-dictation typing that the
  // next partial rebuilds away — the panel (showDictation) already handles the
  // visible streaming case, where the user watches rather than types.
  useEffect(() => {
    if (showDictation || voiceTranscribing) composerControl()?.focus()
  }, [showDictation, voiceTranscribing, composerControl])

  // Discarding a drain from the strip's own button removes the element the press
  // happened on: the discard clears `draining`, so `voiceDrainCancellable` goes
  // false and the strip unmounts with the focused button inside it. The effect
  // above cannot catch that -- a streaming drain has already cleared `recording`
  // (so `showDictation` is null) and `voiceTranscribing` is the batch flag -- and
  // focus would land on the document body, leaving the composer deaf to the very
  // keyboard and touch users this control was added for. Hand focus back as part
  // of the discard rather than on an effect edge, so it is the same press.
  //
  // Stays `undefined` when there is no discard to run, because the strip reads
  // the handler's presence as one of the two terms deciding whether to offer the
  // control at all: wrapping unconditionally would put a button on screen whose
  // only effect is to move focus.
  const cancelVoiceDrain = useMemo(
    () => onVoiceCancel
      ? () => {
        onVoiceCancel()
        composerControl()?.focus()
      }
      : undefined,
    [onVoiceCancel, composerControl],
  )

  // Escape CANCELS dictation (discards the audio), from ANYWHERE. Deliberately a
  // document-level listener rather than the textarea's onKeyDown: starting a
  // recording means clicking the mic button, so focus sits on that button and a
  // textarea-scoped handler never fires — the panel would advertise "Esc to
  // cancel" and do nothing. This DISCARDS: nothing is transcribed or inserted,
  // so an abandoned dictation is thrown away. Clicking the mic remains the
  // commit path (stop + transcribe).
  //
  // It also covers the post-release DRAIN, not just a live recording: a streaming
  // release reports capture over (`voiceRecording` false) while the utterance
  // stays queued behind a cold model, so keyed on `voiceRecording` alone this
  // listener unbinds at the release and the composer locks with no keyboard way
  // out (#13500). `voiceDrainCancellable` is that drain window. Through it the
  // only action is the discard: the `onVoiceToggle` fallback (stop + discard a
  // live recording) is withheld once capture has ended, since toggling there
  // would open a fresh recording — so a drain with no `onVoiceCancel` binds
  // nothing rather than the wrong action.
  //
  // BUBBLE phase, not capture, and it yields three ways. Capture phase runs
  // before every descendant, so an open menu/popover/selector (this composer
  // has many) would lose its own Escape to this handler — recording would stop
  // and the menu would stay open. Bubbling lets the innermost control consume
  // Escape first; Radix and friends call preventDefault() when they do, which
  // is what `defaultPrevented` detects. `anyPickerOpenRef` covers the
  // hand-rolled pickers that close on Escape WITHOUT preventing default, so
  // they cannot be detected that way.
  //
  // The `[role="dialog"]` probe is the precedence rule: Escape belongs to the
  // TOPMOST dismissible surface, and the composer is not it while a dialog is
  // up. Modal, CommandPalette and SnipOverlay all bind Escape on `window` and
  // all carry role="dialog", so one presence check defers to every one of them
  // rather than enumerating them. Without it this handler would steal Escape
  // from each — those surfaces own Escape, so intercepting it here would be a
  // regression, not a trade.
  //
  // stopPropagation() only once we have decided the key is OURS. document
  // bubbles on to `window`, and those window handlers do not check
  // defaultPrevented, so a snip started during recording would otherwise be
  // cancelled by the same keypress that stopped the recording.
  useEffect(() => {
    // A drain is discarded through `onVoiceCancel` alone; a live recording may
    // also fall back to `onVoiceToggle`, which stops-and-discards it.
    const cancel = onVoiceCancel || (voiceRecording ? onVoiceToggle : undefined)
    if ((!voiceRecording && !voiceDrainCancellable) || !cancel) return
    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing || e.defaultPrevented) return
      if (anyPickerOpenRef.current) return
      if (document.querySelector('[role="dialog"]')) return
      e.preventDefault()
      e.stopPropagation()
      cancel()
    }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [voiceRecording, voiceDrainCancellable, onVoiceCancel, onVoiceToggle, anyPickerOpenRef])

  return { publishLexicalSelection, recordCaret, showDictation, cancelVoiceDrain }
}

/** Hold-to-talk: on a touch device the mic switches the composer between the
 *  keyboard and a press-and-hold target, and every label on those controls
 *  says what the next press actually does. */
export function useHoldToTalk({ onVoiceStart, onVoiceStop, onVoiceCancel, voiceCaptureActive, voiceRecording, voiceTranscribeActive, voiceTranscribing, voiceDownload, voiceBusyElsewhere, voiceBusyElsewhereSession, composerHasDraft, disabled, optimizing, placeholder, showDictation }: {
  onVoiceStart: VoiceInput['onVoiceStart']
  onVoiceStop: VoiceInput['onVoiceStop']
  onVoiceCancel: VoiceInput['onVoiceCancel']
  voiceCaptureActive: VoiceInput['voiceCaptureActive']
  voiceRecording: boolean
  voiceTranscribeActive: VoiceInput['voiceTranscribeActive']
  voiceTranscribing: boolean
  voiceDownload: VoiceInput['voiceDownload']
  voiceBusyElsewhere: boolean
  voiceBusyElsewhereSession: VoiceInput['voiceBusyElsewhereSession']
  /** The composer holds text or files a send would carry (session refs excluded). */
  composerHasDraft: boolean
  disabled: boolean
  optimizing: boolean
  placeholder: string
  showDictation: VoiceInput['voiceSampleRef'] | null
}) {
  /**
   * Hold-to-talk mode: the textarea is swapped for a press-and-hold target and
   * the mic button becomes the switch between the two.
   *
   * TOUCH ONLY, and that means the POINTER CLASS — not the width. Desktop already
   * has keyboard push-to-talk (`usePushToTalk`), so a pointer-hold mode there
   * would be a second way to do one thing, and this gesture exists precisely
   * because a thumb has no Esc key to discard with. A narrowed desktop window is
   * still a mouse: including `isMobile` here handed it the mode switch and took
   * away click-to-record, which is the opposite of an addition. `directFilePicker`
   * in the composer pairs the two predicates because a native file dialog genuinely is a
   * width call; this one is not, so it gates on the pointer alone.
   *
   * Resolved inline rather than through a second coarse-pointer subscription: the
   * pointer class does not change under a mounted composer.
   */
  const voiceModeAvailable = !!onVoiceStart && !!onVoiceStop && !!onVoiceCancel && isTouchDevice()
  const [voiceModePref, setVoiceModePref] = useState(() => localStorage.getItem(VOICE_MODE_LS_KEY) === '1')
  /**
   * A draft SUSPENDS hold mode instead of exiting it, so the preference survives.
   *
   * This is the state every finished dictation lands in: the transcript arrives
   * in `value`, and reading, fixing and sending it are all things a hold target
   * cannot do. Suspending hands the textarea back for exactly as long as there is
   * something in it, then returns the hold bar without the user re-choosing it.
   *
   * When a capture the touch gesture OWNS is in flight, the draft check is
   * overridden — the mechanics and the reason live with `voiceHoldMode` below.
   */
  /** "Is capture in flight at all" — see the `voiceCaptureActive` prop doc. Falls
   *  back to the gated flag so the prop stays optional for other callers. */
  const captureInFlight = voiceCaptureActive ?? voiceRecording
  /** "Is a transcription in flight at all" — see the `voiceTranscribeActive` prop
   *  doc. Falls back to the gated flag so the prop stays optional. */
  const transcribeInFlight = voiceTranscribeActive ?? voiceTranscribing
  /** Whether the composer may say "Transcribing". False while the speech model is
   *  still fetching or loading: the status strip directly above already names that
   *  stage, and a placeholder asserting transcription under a line that reads
   *  "Downloading the speech model: 40%" tells the user two different things about
   *  the same wait. The strip is the single source of truth for it, so the
   *  placeholder falls through to its default instead. */
  const transcribingIsHonest = transcribeInFlight && !voiceDownload
  /** Another composer holds the microphone. Blocks STARTING here exactly like a
   *  foreign transcription does, but it is not transcription — nothing of this
   *  composer's is in flight — so it gets its own label and icon, never the
   *  "Transcribing" spinner (UX review on #9787). */
  const micHeldElsewhere = voiceBusyElsewhere && !transcribeInFlight
  const micBlocked = transcribeInFlight || voiceBusyElsewhere
  // Name the chat that holds the mic when the slot list knows it. In a lone DM
  // thread nothing else on screen shows which chat is capturing, so without a
  // name the user cannot go and end it.
  const micOwnerTitle = useAppSelector(s =>
    voiceBusyElsewhereSession ? s.dashboard.slots.find(x => x.key === voiceBusyElsewhereSession)?.title ?? null : null)
  const micHeldElsewhereLabel = micOwnerTitle
    ? i18nT('components.chatInput.mic_in_use_in', { chat: micOwnerTitle })
    : i18nT('components.chatInput.mic_in_use_elsewhere')
  /** State, not a ref: the hold target mounts only once hold mode is on, and the
   *  gesture hook can only bind its listeners when that arrival is observable.
   *  Declared above `touchPtt` because the hook binds to it. */
  const [holdTarget, setHoldTarget] = useState<HTMLButtonElement | null>(null)
  const touchVoice = useMemo(
    () => ({
      recording: captureInFlight,
      start: onVoiceStart ?? noopVoiceControl,
      stop: onVoiceStop ?? noopVoiceControl,
      cancel: onVoiceCancel ?? noopVoiceControl,
    }),
    [captureInFlight, onVoiceStart, onVoiceStop, onVoiceCancel],
  )
  /*
   * `disabled` deliberately omits `!voiceHoldMode`, and that omission is what
   * lets `voiceHoldMode` read the hook's ownership below without a cycle. The
   * term is implied rather than lost: the hook binds only to `holdTarget`, the
   * only writer of `holdTarget` is the hold bar's ref, and the bar renders under
   * `voiceHoldMode &&` — so outside hold mode the hook has no element, no
   * listeners, and nothing left to disable. Leaving hold mode unmounts the bar,
   * which clears the target and runs the hook's own abandon path.
   */
  const touchPtt = useTouchPushToTalk(touchVoice, {
    target: holdTarget,
    disabled: disabled || micBlocked || optimizing,
  })
  /*
   * A draft suspends hold mode, EXCEPT while the touch gesture's own capture is
   * still running — otherwise a transcript landing in the composer would unmount
   * the bar from under the finger that is still holding it.
   *
   * `touchPtt.owns` is what distinguishes the gesture's capture from any other,
   * and it has to be asked: `captureInFlight` alone also matches capture opened
   * elsewhere — the mic-as-record-button, or the keyboard push-to-talk binding
   * on a coarse-pointer device that also has a hardware keyboard. The previous
   * proxy, `holdTarget !== null`, could not tell those apart either: the bar is
   * mounted for EVERY capture that happens while hold mode is on, so a keyboard
   * dictation whose streaming partial landed in the composer kept hold mode
   * alive and rendered a disabled `settling` bar beside a disabled mode switch —
   * two dead touch controls describing a capture neither of them owned (#5753).
   * Ownership comes from the hook's own state machine instead, recorded at the
   * pointerdown that opens capture and relinquished when the gesture resolves.
   *
   * Relinquished AT THE RELEASE, deliberately: a draft the gesture itself
   * streamed in drops hold mode the moment the finger lifts, and the mic — a
   * record toggle again once hold mode drops — is the live stop control for
   * whatever drain remains. The old proxy instead held the surface as a
   * disabled `settling` bar until capture fully ended: a window where nothing
   * on screen was pressable. (What is VISIBLE through that drain depends on the
   * dictation panel: its own gate reads `voiceRecording`, so when enabled — the
   * default — it stays up and the textarea returns when capture ends; the
   * panel's `gestureDriven` carries the settling term for the same window, see
   * the render site.)
   */
  const voiceHoldMode = voiceModeAvailable && voiceModePref
    && (!composerHasDraft || (captureInFlight && touchPtt.owns))
  /**
   * True when the mic press changes MODE rather than starting a recording.
   *
   * ONE predicate for the label, the icon, the action and the disabled state. It
   * is written as a single value because deriving them separately is how a control
   * comes to say one thing and do another: with `!composerHasDraft` alone, a
   * streaming partial landing mid-capture made the label read "Switch to keyboard"
   * (which keys off `voiceHoldMode`, still true because capture overrides the
   * draft) while the click ran `onVoiceToggle` and stopped the recording.
   *
   * `voiceHoldMode ||` is the fix and it is not redundant: hold mode being ON is
   * itself proof there is a mode to switch out of, draft or no draft. The
   * `!composerHasDraft` half covers the other direction — an empty composer with
   * the preference off, where the switch is how voice gets turned on.
   */
  const micIsModeSwitch = voiceModeAvailable && (voiceHoldMode || !composerHasDraft)
  /**
   * Capture is winding DOWN: the gesture is over but the transport has not let go.
   *
   * Streaming `stop()` keeps `recording` true until its socket is cleaned up, and
   * `transcribeInFlight` is still false through that drain — so the bar fell back to
   * "Hold to talk" while enabled, and the next press hit the hook's
   * existing-recording branch and STOPPED the phantom session instead of opening a
   * new one. The user's next utterance was simply not captured.
   *
   * Derived from `touchPtt.bar` and used only for the label and the button's
   * `disabled` — deliberately NOT fed back into the hook's own `disabled`, which
   * would be circular. It does not need to be: a disabled <button> dispatches no
   * pointer events, so the gesture cannot start from a bar that is switched off.
   */
  const voiceSettling = voiceHoldMode && touchPtt.bar === 'settling'
  /**
   * The textarea is PARKED: still mounted, but clipped out of layout by the
   * `sr-only` box the hold bar and the dictation panel both put it in.
   *
   * Anything that measures the textarea has to ask this first — see `applyHeight`
   * for what a 1px-wide measurement did to the composer's height. It also has to
   * be a dep of those effects, so the height is recomputed on the way BACK: the
   * value that was streamed in while parked is exactly the value whose height was
   * never measurable.
   */
  const textareaParked = !!showDictation || voiceHoldMode
  const toggleVoiceMode = useCallback(() => {
    setVoiceModePref(prev => {
      const next = !prev
      safeSetItem(VOICE_MODE_LS_KEY, next ? '1' : '0')
      return next
    })
  }, [])
  /**
   * One label for the mic button, which is two different controls depending on
   * the device: a mode SWITCH on touch, the record toggle everywhere else.
   *
   * The draft case is spelled out rather than left as a bare greyed button —
   * "why can I not press this" is otherwise unanswerable, and the answer (there
   * is unsent text in the composer) is something the user can act on.
   */
  // Branches on `micIsModeSwitch` FIRST, so the label can only ever describe the
  // job the click actually performs. Reading `voiceHoldMode` first was what let the
  // two diverge — and a transcription elsewhere must not relabel a control whose
  // only job here is handing the keyboard back.
  const micLabel = micIsModeSwitch
    ? voiceHoldMode
      ? i18nT('components.chatInput.switch_to_keyboard')
      : i18nT('components.chatInput.switch_to_voice')
    : transcribeInFlight
      ? i18nT('components.chatInput.transcribing')
      : micHeldElsewhere
        ? micHeldElsewhereLabel
      // Not a switch: it records. Same two labels the desktop mic has always had.
      : voiceRecording
        ? i18nT('components.chatInput.stop_recording')
        : i18nT('components.chatInput.voice_input')
  /**
   * What the hold bar says, which must describe what the NEXT press or release
   * ACTUALLY does. Two of these were wrong for the same reason — the WeChat
   * gesture this copies sends on release, and the labels borrowed its promises
   * without borrowing its behaviour:
   *
   * - Releasing does NOT send. `stopVoice` sets `sttEndpointDisarmedRef` on
   *   purpose, so a manual stop cannot become an unrequested send; the transcript
   *   arrives as a composer draft. A user trusting "Release to send" would release,
   *   pocket the phone, and never notice the message was still sitting there — a
   *   silent failure on a chat surface's core action. It says `Release to
   *   transcribe`, which is what release does.
   * - While transcribing, the bar is disabled and used to still read "Hold to
   *   talk", so the dead control explained nothing.
   */
  const holdBarLabel = transcribeInFlight
    ? i18nT('components.chatInput.transcribing')
    : touchPtt.bar === 'settling'
      // NOT "Transcribing": the drain has not handed anything to the transcriber
      // yet. Saying so would claim work that has not started — the same overclaim
      // this bar has already been corrected for twice.
      ? i18nT('components.chatInput.finishing')
      : touchPtt.bar === 'armed-cancel'
        ? i18nT('components.chatInput.release_to_cancel')
        : touchPtt.bar === 'holding'
          ? i18nT('components.chatInput.release_to_transcribe')
          : touchPtt.bar === 'tap-too-short'
            ? i18nT('components.chatInput.keep_holding_to_record')
            : i18nT('components.chatInput.hold_to_talk')
  /**
   * Discovery hint for the mic switch, shown only where the switch exists and
   * only while it is reachable.
   *
   * Deliberately ranked BELOW `continuePlaceholder` and below a caller-supplied
   * `placeholder`: the resume hint is about a broken turn and outranks a feature
   * tour, and a caller that named its own placeholder means it.
   *
   * It names where the mic LEADS, not an action to perform. Two earlier wordings
   * were both wrong for the same reason — a two-step affordance does not fit in one
   * line, and compressing it produced a promise the tap does not keep:
   *
   * - "hold to talk" named a gesture with no target in keyboard mode (the textarea
   *   cannot be one, since a long press there opens the iOS selection loupe).
   * - "tap the mic to talk" was worse: the tap runs `toggleVoiceMode` and starts no
   *   capture, so anyone who tapped and spoke was not recorded at all.
   *
   * So it promises only what the tap delivers — voice becomes available — and the
   * hold bar that appears teaches the gesture where the gesture actually exists.
   */
  const voiceModePlaceholder = voiceModeAvailable && !voiceHoldMode && !composerHasDraft && !placeholder
    ? i18nT('components.chatInput.send_a_message_or_tap_the_mic_for_voice')
    : ''

  return {
    transcribeInFlight, transcribingIsHonest, micHeldElsewhere, micBlocked, micOwnerTitle, micHeldElsewhereLabel,
    setHoldTarget, touchPtt, voiceHoldMode, micIsModeSwitch, voiceSettling, textareaParked, toggleVoiceMode, micLabel, holdBarLabel,
    voiceModePlaceholder,
  }
}
