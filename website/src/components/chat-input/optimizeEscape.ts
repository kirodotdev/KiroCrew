import { useCallback } from 'react'
import { anotherEscapeOwnerIsOpen } from './escapeOwners'
import { escapeStoppedSpeech } from '../../utils/speechStopKey'

/* Escape cancels an in-flight prompt optimize, the way its Cancel button does.
   Escape never stops a running turn: the Stop button is the one way to stop.

   Each composer editor calls this gate on a bare Escape: the textarea's
   keydown (keyboard.ts), the rich-text editor's Escape command
   (LexicalComposerInput), and the composer's Escape-stack layer, which answers
   while an optimize locks the rich-text editor and it drops keys. The gate
   yields to whatever else in the composer owns Escape first. */

/** The part of an Escape keydown the gate reads. Both React's synthetic event
 *  (textarea) and the native event Lexical hands its Escape command fit it. */
export type ComposerEscapeEvent = Pick<KeyboardEvent, 'preventDefault'>

/** What the calling editor knows about this keydown beyond the event itself. */
export interface ComposerEscapeContext {
  /** The native keydown (the textarea passes `e.nativeEvent`). */
  native: Event
  /** The calling editor's own IME verdict: each editor tracks composition
   *  through its own latch. */
  composing: boolean
}

/** Run on a bare Escape. Returns true when the gate claimed the key. */
export type ComposerEscapeKey = (e: ComposerEscapeEvent, key: ComposerEscapeContext) => boolean

export function useComposerOptimizeEscape({ anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, optimizingRef, onCancelOptimize }: {
  /** Any trigger picker (/ @ $ path) is open: it owns Escape to close. */
  anyPickerOpenRef: React.RefObject<boolean>
  /** A dictation is recording or draining: voice.ts owns Escape to discard. */
  voiceRecording: boolean
  voiceDrainCancellable: boolean
  /** A pending tool approval is shown: its own Escape handling owns the key. */
  hasApproval: boolean
  /** The approval-mode nudge is up: Escape hides it for the session. */
  approvalNudgeActive: boolean
  /** A prompt optimize is in flight. */
  optimizingRef?: React.RefObject<boolean>
  onCancelOptimize?: () => void
}): ComposerEscapeKey {
  return useCallback((e: ComposerEscapeEvent, { native, composing }: ComposerEscapeContext): boolean => {
    // Escape that only cancels an IME candidate window, or that already
    // stopped voice read-back, is spent.
    if (composing || escapeStoppedSpeech(native)) return false
    if (anyPickerOpenRef.current) return false
    if (voiceRecording || voiceDrainCancellable) return false
    if (hasApproval || approvalNudgeActive) return false
    if (anotherEscapeOwnerIsOpen()) return false
    if (!optimizingRef?.current || !onCancelOptimize) return false
    e.preventDefault()
    onCancelOptimize()
    return true
  }, [anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, optimizingRef, onCancelOptimize])
}
