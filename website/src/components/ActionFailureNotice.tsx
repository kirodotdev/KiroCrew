/**
 * ActionFailureNotice — the one rendered sink for `reportActionFailure`.
 *
 * Mounted in the always-mounted shells (the dashboard's `<main>`, the embed
 * frame, the popout frame), NOT in a page. The store it reads is module-level so
 * a write reported from a menu subtree that unmounts still has somewhere to
 * land — but that only holds if the sink itself outlives the reporter. While the
 * sink lived in `ChatPage`, a session write issued from the global command
 * palette on a non-chat route (Settings + "Pin current session") reverted and
 * reported into a page that was not mounted, so the rollback was silent: the
 * dead end `errors-use-error-notice` forbids. Route-independent for the same
 * reason `CrashReportNotice` beside it is — a rejected session write is true of
 * the app, not of the page the reader happens to be on.
 */
import ErrorNotice from './ErrorNotice'
import { useGuardedLeave } from './NavigationLeaveGuard'
import { useActionFailure } from '../utils/actionFailure'
import { i18nT } from '../i18n/t'

export default function ActionFailureNotice({ className = 'mx-4 mt-2 mb-0 animate-rise' }: {
  /** Shell-specific spacing; the default matches the dashboard's other banners. */
  className?: string
}) {
  const { failure, earlier, clear } = useActionFailure()
  const leave = useGuardedLeave()
  return (
    <ErrorNotice
      title={failure?.heading ?? (failure?.subject
        ? i18nT('pages.chatPage.could_not_update', { name: failure.subject })
        : i18nT('components.actionFailureNotice.unnamed_heading'))}
      message={failure?.message ?? ''}
      report={failure?.report}
      // Without it, dismissing one failure and seeing another take its place
      // reads as a dismiss that did not work.
      footer={earlier > 0 ? i18nT('components.actionFailureNotice.earlier_waiting', { count: earlier }) : null}
      askAgent
      // The hand-off always targets /chat; same-route clicks unmount nothing and should not prompt.
      askAgentGate={proceed => { leave(proceed, '/chat') }}
      onDismiss={clear}
      className={className}
      testId="session-action-error"
    />
  )
}
