import { useCallback } from 'react'
import { anotherEscapeOwnerIsOpen } from './escapeOwners'
import { escapeStoppedSpeech } from '../../utils/speechStopKey'

/* Escape-to-stop for the composer (issue #22315). The CLI binds Esc to cancel
   the streaming turn; the dashboard had only the Stop button. This hook is the
   PRECEDENCE GATE for that binding: each composer editor calls it on a bare
   Escape, and it stops the running turn only when nothing else in the composer
   owns Escape first. It is deliberately NOT a page-wide shortcut. It is wired
   into the editors' own key handling (the textarea's keydown in keyboard.ts,
   the rich-text editor's Escape command in LexicalComposerInput), so a
   dialog, the command bar, a terminal or the transcript holding focus never
   reaches it. The one page-level caller is the composer's Escape-stack layer
   (hooks/useEscapeLayer), registered only while an optimize runs, where the
   locked rich-text editor drops keys; there it can only cancel the optimize. */

/** The part of an Escape keydown the gate reads. Both React's synthetic event
 *  (textarea) and the native event Lexical hands its Escape command fit it. */
export type StopKeyEvent = Pick<KeyboardEvent, 'repeat' | 'preventDefault'>

/** What the calling editor knows about this keydown beyond the event itself. */
export interface StopKeyContext {
  /** The native keydown (the textarea passes `e.nativeEvent`). */
  native: Event
  /** The calling editor's own IME verdict: each editor tracks composition
   *  through its own latch. */
  composing: boolean
}

/** Run on a bare Escape. Returns true when the gate claimed the key. */
export type StopTurnOnEscape = (e: StopKeyEvent, key: StopKeyContext) => boolean

export function useComposerStopKey({ isRunning, onStop, anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, optimizingRef, onCancelOptimize }: {
  /** A turn is running on THIS composer's slot. */
  isRunning: boolean
  /** The Stop protocol the button runs (soft cancel, then force kill on a
   *  second press after the arm window). Absent on a surface with no stop. */
  onStop?: () => void
  /** Any trigger picker (/ @ $ path) is open: it owns Escape to close. */
  anyPickerOpenRef: React.RefObject<boolean>
  /** A dictation is recording or draining: voice.ts owns Escape to discard. */
  voiceRecording: boolean
  voiceDrainCancellable: boolean
  /** A pending tool approval is shown: its own Escape handling owns the key. */
  hasApproval: boolean
  /** The approval-mode nudge is up: Escape hides it for the session. */
  approvalNudgeActive: boolean
  /** A prompt optimize is in flight: Escape cancels it, as its Cancel button does. */
  optimizingRef?: React.RefObject<boolean>
  onCancelOptimize?: () => void
}): StopTurnOnEscape {
  return useCallback((e: StopKeyEvent, { native, composing }: StopKeyContext): boolean => {
    // Leave Escape alone when an IME composition owns the key (Escape cancels
    // the candidate window, never the turn). The caller computes `composing`
    // from its own latch, which also catches the post-`compositionend` WebKit
    // keydown (keyCode 229, isComposing already false).
    if (composing) return false
    // This same keydown already stopped voice read-back (the capture-phase
    // stop-speaking shortcut). The press was for the speech: stopping the turn
    // too would soft-cancel it, and a second press to silence the next
    // sentence would force-kill it. The store cannot answer this, because
    // stopping speech clears `voicePlaying` before the key gets here.
    if (escapeStoppedSpeech(native)) return false
    // Precedence: Escape belongs to what owns it in the composer today, and
    // the turn is the LAST claimant. Each of these consumes Escape itself:
    //  - an open trigger picker closes (anyPickerOpenRef; some close without
    //    preventDefault, so the ref is the only reliable signal);
    //  - a recording or draining dictation discards (voice.ts);
    //  - a pending approval / its mode nudge dismisses (approval.ts);
    //  - voice read-back stops (checked above, on the event itself).
    if (anyPickerOpenRef.current) return false
    if (voiceRecording || voiceDrainCancellable) return false
    if (hasApproval || approvalNudgeActive) return false
    // Any other surface that owns Escape takes it over the composer: the same
    // topmost-dismissible-surface rule voice.ts uses, asked through the shared
    // probe so the two gates cannot drift (escapeOwners.ts documents exactly
    // which surfaces count and why).
    if (anotherEscapeOwnerIsOpen()) return false
    // An in-flight prompt optimize comes next: Escape cancels it the way its
    // Cancel button does, and this press stops nothing else.
    if (optimizingRef?.current && onCancelOptimize) {
      e.preventDefault()
      onCancelOptimize()
      return true
    }
    // No turn to stop, or no stop on this surface.
    if (!isRunning || !onStop) return false
    // The key is ours. But a HELD Escape must never escalate: after the first
    // soft cancel the slot is optimistically `soft_pending`, and a keydown more
    // than FORCE_KILL_ARMING_MS (400ms) later is read as a force kill by the
    // shared stop protocol, which discards the in-flight turn AND any queued
    // messages. OS key-repeat starts well inside that window, so holding Escape
    // a moment too long would hard-kill from one press. Swallow an auto-repeat
    // keydown (claim it so it reaches nothing else) without acting: escalation
    // then requires a deliberate release-and-press, exactly like the button.
    if (e.repeat) { e.preventDefault(); return true }
    // Stop the browser's default Escape (blur / clear) and run the same Stop
    // the button runs. preventDefault also tells document-level handlers that
    // read `defaultPrevented` (the notification banner, dictation discard)
    // that this Escape is spent. A SECOND deliberate Escape after the arm
    // window force-kills, because onStop carries the press protocol.
    e.preventDefault()
    onStop()
    return true
  }, [isRunning, onStop, anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, optimizingRef, onCancelOptimize])
}
