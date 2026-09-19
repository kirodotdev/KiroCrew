import ErrorNotice from './ErrorNotice'
import { agentSwitchFailureReport, agentSwitchOffersHandoff } from '../utils/agentSwitchFeedback'

/**
 * The agent-switch result toast.
 *
 * Why this surface changed AT ALL, rather than for consistency: this change adds a refusal that
 * did not exist before -- a switch can now answer `503 workspace_unavailable` when the configured
 * root is unreadable -- and this component is its ONLY renderer (mounted once, in `App`). That
 * refusal is one the user cannot act on: no retry, no field to correct, the directory is simply
 * not there. The shared `ErrorNotice` is the one place that recovers the structured context an
 * error carries (route, endpoint, status, backend `code`) and offers it to the agent, so it is
 * what keeps the new refusal from being a dead end. Bespoke markup rendered the sentence and
 * discarded the `code` this change introduced, which is the part a recovery needs.
 *
 * That recovery is NOT automatic on this surface, which is why `report` is passed rather than
 * left to the component: `ErrorNotice` finds the context by looking the journal up on the
 * message, and this surface's messages are localized replacements for the backend's prose, so
 * the key never matches. `agentSwitchFailureReport` resolves it on the wire contract instead.
 *
 * This wrapper owns only what a FLOATING notice needs and the shared component cannot know --
 * viewport anchoring, elevation, and an OPAQUE backdrop, since the notice's own tint is
 * translucent and would otherwise read against whatever it covers.
 *
 * Every outcome here keeps the danger palette and `role="alert"`, the two recognized refusals
 * included. A withheld switch is still a REJECTED REQUEST carrying a backend `{ error, code }`
 * body, and `errors-use-error-notice` (blocking) names a warn-toned or `role="status"` failure as
 * the same violation as a hand-rolled red div: toning a failure down does not make it status.
 *
 * The hand-off is GATED, not merely opted into. This notice owns no editable field, but it floats
 * over the whole app: the switch is reachable from a global Alt+Shift cycle that is not input-gated,
 * so an unsaved draft can be mounted UNDER it. `askAgent` only decides the button exists; without a
 * `gate` the hand-off soft-navigates to `/chat` directly and unmounts that subtree, destroying the
 * draft with no prompt. `useGuardedLeave` asks the leave guard first and vetoes the navigation when
 * the user chooses to keep editing.
 */
export default function AgentSwitchNotice({
  message,
  onDismiss,
  gate,
}: {
  /** Resolved by `agentSwitchFailureMessage`; falsy renders nothing. */
  message?: string | null
  onDismiss: () => void
  /**
   * The leave guard the hand-off must clear, threaded from `App` rather than read here with
   * `useGuardedLeave`: that hook needs a Router in context, and keeping this wrapper free of
   * routing context is what lets it be unit-rendered bare.
   */
  gate?: (proceed: () => void) => void
}) {
  if (!message) return null
  return (
    <div
      data-testid="agent-switch-notice"
      className="fixed z-[70] top-safe-offset-14 left-safe-offset-4 right-safe-offset-4 sm:left-auto sm:w-[440px] bg-bg-elevated rounded-lg shadow-xl animate-rise"
    >
      {/* No hand-off while a turn is IN FLIGHT: the hand-off creates and activates a new
        * session, so it would move the user off the very turn this notice tells them to wait
        * for -- the running turn is the state it protects, and that turn clears itself. An
        * unavailable WORKSPACE has no such turn and does need the agent, so it keeps it. */}
      <ErrorNotice
        message={message}
        report={agentSwitchFailureReport(message)}
        askAgent={agentSwitchOffersHandoff(message)}
        gate={gate}
        onDismiss={onDismiss}
        testId="agent-switch-error"
      />
    </div>
  )
}
