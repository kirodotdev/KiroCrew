import type { ComposerControl } from '../composerControl'
import { isTouchDevice } from '../../utils/isTouchDevice'

/**
 * Focus hand-back for the control-row pickers (approval mode, steer/queue)
 * after a PICK (#18313).
 *
 * Both pickers would otherwise leave focus on their own trigger, so the Enter
 * the user means as "send" re-opens the menu. A pick is a finished action whose
 * next step is typing, so the composer takes focus -- through the engine-neutral
 * control, so it lands in whichever composer is live (textarea or Lexical) in
 * THIS pane, not a document-global first match.
 *
 * The return value is the CONTRACT with the picker: `true` means "I hold
 * focus now, suppress your own return-to-trigger"; `false` means "keep it". So
 * it reports where focus actually LANDED, not that it was asked for:
 *  - touch is declined up front (focusing the box pops the keyboard);
 *  - a disabled box (disconnected, optimizing, transcribing) ignores `focus()`,
 *    and a `true` there would strand focus on `<body>` (review finding on
 *    PR #18321).
 * Cancels (Escape, outside click) never reach this; they keep the ARIA
 * menu-button return to the trigger.
 */
export function focusComposerAfterPick(control: ComposerControl | null): boolean {
  if (isTouchDevice()) return false
  if (!control) return false
  control.focus()
  const root = control.getRootElement()
  return !!root && root.contains(document.activeElement)
}
