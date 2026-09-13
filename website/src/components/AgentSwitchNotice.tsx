import ErrorNotice from './ErrorNotice'

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
 * This wrapper owns only what a FLOATING notice needs and the shared component cannot know --
 * viewport anchoring, elevation, and an OPAQUE backdrop, since the notice's own tint is
 * translucent and would otherwise read against whatever it covers.
 *
 * `warn`, not the danger default: both refusals this reports -- a turn in flight, and an
 * unavailable workspace root -- WITHHELD the switch without anything breaking. The hand-off is on
 * because this surface has nothing to lose: it owns no editable field, and the composer draft the
 * navigation passes through is flushed to its per-slot store on unmount.
 */
export default function AgentSwitchNotice({
  message,
  onDismiss,
}: {
  /** Resolved by `agentSwitchFailureMessage`; falsy renders nothing. */
  message?: string | null
  onDismiss: () => void
}) {
  if (!message) return null
  return (
    <div
      data-testid="agent-switch-notice"
      className="fixed z-[70] top-safe-offset-14 left-safe-offset-4 right-safe-offset-4 sm:left-auto sm:w-[440px] bg-bg-elevated rounded-lg shadow-xl animate-rise"
    >
      <ErrorNotice message={message} warn askAgent onDismiss={onDismiss} testId="agent-switch-error" />
    </div>
  )
}
