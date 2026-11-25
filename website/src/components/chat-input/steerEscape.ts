import { useCallback } from 'react'
import { anotherEscapeOwnerIsOpen } from './escapeOwners'
import { escapeStoppedSpeech } from '../../utils/speechStopKey'
import type { ComposerEscapeKey } from './optimizeEscape'

/* Escape takes back a steer the running turn has not read yet (issue #42197).
   A steer sent too early, or with a mistake in it, can be taken back from its
   row (Queue instead, and Edit and Unsend in its menu). This gate adds the
   keyboard route: each bare Escape in the message box takes back the newest
   pending steer, with Edit, and its text goes in front of the draft, so one
   press per steer rebuilds the steers in the order they were sent.

   Escape never stops the turn
   (docs/decisions/2026-11-24-escape-in-the-message-box-does-not-stop-the-turn.md).
   With no steer to take back this gate claims nothing, and the press does what
   it does on a composer with no steers. The gate runs behind the optimize gate
   (optimizeEscape.ts) and yields to the same claimants, in the same order: a
   press that closes a picker, discards a dictation, dismisses an approval or
   cancels an optimize takes nothing back. */

/** The page's steers that the running turn has not read yet, as Escape sees them. */
export interface SteerEscape {
  /** Pending steers whose row offers a take-back and that no press has asked for yet. */
  offered: number
  /** `offered`, plus the composer's own steers still waiting for their echo. */
  reachable: number
  /** Takes back the newest steer Escape can still reach. False when there is none. */
  takeBack: () => boolean
}

export function useComposerSteerEscape({ optimizeEscape, steerEscape, composerReadOnly, anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, optimizingRef }: {
  /** The gate ahead of this one: it cancels an in-flight optimize. */
  optimizeEscape: ComposerEscapeKey
  /** The slot's steers the turn has not read yet. Absent on a surface that
   *  cannot take a steer back. */
  steerEscape?: SteerEscape
  /** The message box takes no text now (an optimize, a typing lock, a streaming
   *  dictation, a disabled composer): the hint hides, since the text a press
   *  brings back could not be edited. */
  composerReadOnly: boolean
  /** Any trigger picker (/ @ $ path) is open: it owns Escape to close. */
  anyPickerOpenRef: React.RefObject<boolean>
  /** A dictation is recording or draining: voice.ts owns Escape to discard. */
  voiceRecording: boolean
  voiceDrainCancellable: boolean
  /** A pending tool approval is shown: its own Escape handling owns the key. */
  hasApproval: boolean
  /** The approval-mode nudge is up: Escape hides it for the session. */
  approvalNudgeActive: boolean
  /** A prompt optimize is in flight: it is rewriting the draft the steer's text
   *  would join, so the press takes nothing back. */
  optimizingRef?: React.RefObject<boolean>
}): { composerEscape: ComposerEscapeKey; takeBackHint: boolean } {
  const composerEscape = useCallback<ComposerEscapeKey>((e, key) => {
    if (optimizeEscape(e, key)) return true
    // The claimants the optimize gate yields to, in the same order.
    if (key.composing || escapeStoppedSpeech(key.native)) return false
    if (anyPickerOpenRef.current) return false
    if (voiceRecording || voiceDrainCancellable) return false
    if (hasApproval || approvalNudgeActive) return false
    if (anotherEscapeOwnerIsOpen()) return false
    if (optimizingRef?.current) return false
    if (!steerEscape?.reachable) return false
    // preventDefault also tells document-level handlers that read
    // `defaultPrevented` (the notification banner, dictation discard) that
    // this Escape is spent.
    e.preventDefault()
    // A held Escape takes back one steer, not one per key repeat: OS key-repeat
    // starts within a few hundred milliseconds, so a press held a moment too
    // long would otherwise empty the whole steer stack. An auto-repeat keydown
    // is claimed and does nothing; the next take-back needs a release and a
    // new press.
    if (e.repeat) return true
    steerEscape.takeBack()
    return true
  }, [optimizeEscape, steerEscape, anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, optimizingRef])
  // The hint says what the next press does, so it hides while a claimant ahead
  // of the steers holds Escape (a dictation, an approval) or the box takes no text.
  const escapeOwned = voiceRecording || voiceDrainCancellable || hasApproval || approvalNudgeActive
  const takeBackHint = !!steerEscape && steerEscape.offered > 0 && !escapeOwned && !composerReadOnly
  return { composerEscape, takeBackHint }
}
