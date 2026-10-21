import ErrorNotice from './ErrorNotice'

/**
 * The agent-switch result toast.
 *
 * Why this surface changed at all: it rendered a rejected request through hand-rolled markup with
 * `role="status"` and a warn-tinted border, and `errors-use-error-notice` (blocking) names a
 * warn-toned or `role="status"` failure as the same violation as a hand-rolled red div. That rule
 * decides by where the VALUE came from, not by how it reads, so a withheld switch counts even
 * though a turn already in flight clears itself: it arrives as a rejected request carrying a
 * backend `{ error, code }` body, which puts it outside the rule's carve-out for status text about
 * something that has not failed. The session-folder notice shares this channel and is also a
 * failure by that test -- the backend sets it only inside `except SessionFolderError` -- so every
 * value reaching here takes the same path.
 *
 * This wrapper owns only what a FLOATING notice needs and the shared component cannot know --
 * viewport anchoring, elevation, and an OPAQUE backdrop, since the notice's own tint is
 * translucent and would otherwise read against whatever it covers.
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
      {/* No hand-off: this notice is `position: fixed` and floats over the whole app, and the
        * switch that raises it is reachable from a global Alt+Shift cycle that is not
        * input-gated -- so the chat composer draft, or any half-filled form, can be mounted
        * UNDERNEATH it. The hand-off navigates to chat and unmounts that subtree, discarding
        * the draft with no prompt, and this surface cannot see what it is covering. */}
      <ErrorNotice
        message={message}
        onDismiss={onDismiss}
        testId="agent-switch-error"
      />
    </div>
  )
}
