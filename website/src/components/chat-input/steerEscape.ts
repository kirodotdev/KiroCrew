import { useCallback, useRef } from 'react'
import { anotherEscapeOwnerIsOpen } from './escapeOwners'
import { escapeStoppedSpeech } from '../../utils/speechStopKey'
import { useComposerOptimizeEscape, type ComposerEscapeKey } from './optimizeEscape'

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
   (optimizeEscape.ts) and yields to the same claimants: a press that closes a
   picker, discards a dictation, dismisses an approval or cancels an optimize
   takes nothing back. It also yields while the message box takes no text,
   because the text a press brings back could be neither seen nor edited. */

/** The page's steers that the running turn has not read yet, as Escape sees them. */
export interface SteerEscape {
  /** Pending steers whose row offers a take-back and that no press has asked for yet. */
  offered: number
  /** `offered`, plus the composer's own steers still waiting for their echo. */
  reachable: number
  /** Takes back the newest steer Escape can still reach. False when there is none. */
  takeBack: () => boolean
}

/** What holds Escape ahead of the steers, apart from the keydown itself. The
 *  gate and its hint read the same set, so the hint shows only while the next
 *  press would take a steer back. */
export interface SteerEscapeClaimants {
  /** Any trigger picker (/ @ $ path) is open: it owns Escape to close. */
  anyPickerOpen: boolean
  /** A dictation is recording or draining: voice.ts owns Escape to discard. */
  voiceRecording: boolean
  voiceDrainCancellable: boolean
  /** A pending tool approval is shown: its own Escape handling owns the key. */
  hasApproval: boolean
  /** The approval-mode nudge is up: Escape hides it for the session. */
  approvalNudgeActive: boolean
  /** The message box takes no text now: an optimize, a typing lock, a streaming
   *  dictation, a disabled composer, or hold-to-talk hiding the box. */
  composerReadOnly: boolean
}

function claimedAhead(c: SteerEscapeClaimants): boolean {
  return c.anyPickerOpen || c.voiceRecording || c.voiceDrainCancellable || c.hasApproval || c.approvalNudgeActive
    || c.composerReadOnly || anotherEscapeOwnerIsOpen()
}

/**
 * Checks whether the composer shows "Esc to take back your message": a row
 * offers a take-back and nothing ahead of the steers holds Escape.
 *
 * A dialog or a side panel view that opens without a composer render hides the
 * hint at the composer's next render; the press itself checks at key time.
 */
export function steerTakeBackHintShown(steerEscape: SteerEscape | undefined, claimants: SteerEscapeClaimants): boolean {
  return !!steerEscape && steerEscape.offered > 0 && !claimedAhead(claimants)
}

/** The composer's bare-Escape gate: the optimize gate, then the take-back. */
export function useComposerEscape({ steerEscape, composerReadOnlyRef, ...optimize }: Parameters<typeof useComposerOptimizeEscape>[0] & {
  /** The slot's steers the turn has not read yet. Absent on a surface that
   *  cannot take a steer back. */
  steerEscape?: SteerEscape
  /** `SteerEscapeClaimants.composerReadOnly` as of the latest render. */
  composerReadOnlyRef: React.RefObject<boolean>
}): ComposerEscapeKey {
  const optimizeEscape = useComposerOptimizeEscape(optimize)
  const { anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, optimizingRef } = optimize
  // Whether the press that started the current hold took a steer back. A held
  // Escape takes back one steer, not one per key repeat: OS key-repeat starts
  // within a few hundred milliseconds, so a press held a moment too long would
  // otherwise empty the whole steer stack. The repeats of such a hold are
  // claimed and do nothing, even once no steer is left; the next take-back
  // needs a release and a new press. The repeats of any other hold pass on as
  // they do on a composer with no steers.
  const holdTookBackRef = useRef(false)
  return useCallback<ComposerEscapeKey>((e, key) => {
    if (e.repeat) {
      if (!holdTookBackRef.current) return optimizeEscape(e, key)
      e.preventDefault()
      return true
    }
    holdTookBackRef.current = false
    if (optimizeEscape(e, key)) return true
    if (key.composing || escapeStoppedSpeech(key.native)) return false
    const claimants = { anyPickerOpen: !!anyPickerOpenRef.current, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, composerReadOnly: !!composerReadOnlyRef.current }
    // An in-flight optimize is rewriting the draft the steer's text would join.
    if (claimedAhead(claimants) || optimizingRef?.current) return false
    if (!steerEscape?.reachable || !steerEscape.takeBack()) return false
    // preventDefault also tells document-level handlers that read
    // `defaultPrevented` (the notification banner, dictation discard) that
    // this Escape is spent.
    e.preventDefault()
    holdTookBackRef.current = true
    return true
  }, [optimizeEscape, steerEscape, composerReadOnlyRef, anyPickerOpenRef, voiceRecording, voiceDrainCancellable, hasApproval, approvalNudgeActive, optimizingRef])
}
