import type { AppDispatch } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'

/**
 * Report a per-slot setting write that did not persist on BOTH its surfaces.
 *
 * The in-place copies pass `announce={false}` to `ErrorNotice`, so the floating
 * toast is the only one that announces. Writing the two values through one call
 * is what makes that safe: a caller cannot set the silenced copy without the
 * toast, because there is no code path here that sets one alone.
 */
export function reportSlotWriteFailure(
  error: unknown,
  dispatch: AppDispatch,
  setInPlaceMessage: (message: string) => void,
): string {
  const message = agentSwitchFailureMessage(error)
  dispatch(setAgentSwitchNotice(message))
  setInPlaceMessage(message)
  return message
}

/**
 * Clear both surfaces before a retry.
 *
 * The toast is rendered unkeyed, so an identical message is no DOM change and
 * announces nothing; clearing it first is what lets a repeat of the same
 * refusal announce again.
 */
export function clearSlotWriteFailure(
  dispatch: AppDispatch,
  setInPlaceMessage: (message: string) => void,
): void {
  dispatch(setAgentSwitchNotice(null))
  setInPlaceMessage('')
}
