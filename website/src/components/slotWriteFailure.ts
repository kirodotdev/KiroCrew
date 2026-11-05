import type { AppDispatch } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import type { AgentSwitchNotice } from '../utils/agentSwitchFeedback'

/**
 * Report a per-slot setting write that did not persist on BOTH its surfaces.
 *
 * The in-place copies pass `announce={false}` to `ErrorNotice`, so the shell's
 * agent-switch banner is the only one that announces. Writing the two values
 * through one call is what makes that safe: a caller cannot set the silenced
 * copy without the banner, because there is no code path here that sets one alone.
 */
export function reportSlotWriteFailure(
  notice: AgentSwitchNotice,
  dispatch: AppDispatch,
  setInPlaceMessage: (message: string) => void,
): void {
  dispatch(setAgentSwitchNotice(notice))
  setInPlaceMessage(notice.message)
}

/**
 * Clear both surfaces before a retry.
 *
 * An identical message is no DOM change and announces nothing; clearing the
 * banner first is what lets a repeat of the same refusal announce again.
 */
export function clearSlotWriteFailure(
  dispatch: AppDispatch,
  setInPlaceMessage: (message: string) => void,
): void {
  dispatch(setAgentSwitchNotice(null))
  setInPlaceMessage('')
}
