import { useState } from 'react'
import ErrorNotice from '../components/ErrorNotice'
import { Btn } from '../components/ui'
import { i18nT } from '../i18n/t'
import { queryReadFailed } from '../utils/queryReadFailed'

/** The slice of the members roster query this notice reads. */
export interface CrewmateRosterRead {
  isError: boolean
  isPending: boolean
  isFetching: boolean
  errorUpdateCount: number
  error: unknown
  refetch: () => Promise<unknown>
}

/**
 * The Schedule page's failed-Bot-roster notice and its Retry.
 *
 * Keyed on `queryReadFailed`, not `isError`: a retry with no cached roster
 * returns the query to `pending` and clears `error`, so a notice gated on
 * `isError` unmounts itself, its Retry and the keyboard focus on it for the
 * whole retry. The last error is held in state for the same span, so the
 * message stays the failure the user is retrying rather than a blank or
 * generic line.
 */
export default function ScheduleCrewmateRosterNotice({ read, show, errorMessage }: {
  read: CrewmateRosterRead
  /** The page's own conditions: a listed job needs the roster and the job dialog is closed. */
  show: boolean
  errorMessage: (e: unknown) => string
}) {
  const [lastError, setLastError] = useState<unknown>(null)
  if (read.error && read.error !== lastError) setLastError(read.error)
  if (!show || !queryReadFailed(read)) return null
  const shownError = read.error ?? lastError
  // A notice that mounts mid-read (the page opened while the shared query was
  // already failed and refetching) has seen no error yet: it says a read is in
  // progress instead of a generic failure line.
  const message = shownError ? errorMessage(shownError) : i18nT('components.agentSelector.retrying')
  return (
    <div className="flex flex-wrap items-center gap-2 mb-3">
      {/* askAgent on: the job dialog is closed, so no job draft can be lost. */}
      <ErrorNotice
        variant="inline"
        className="flex-1 min-w-0"
        title={i18nT('pages.membersPage.roster_load_failed')}
        message={message}
        askAgent
        testId="schedule-crewmate-roster-error"
      />
      {/* `busy`, NOT `disabled`: Btn keeps a busy button focusable and refuses
          its press, where native `disabled` drops a keyboard user's focus. */}
      <Btn busy={read.isFetching} onClick={() => void read.refetch()}>
        {i18nT('pages.schedulePage.retry')}
      </Btn>
    </div>
  )
}
