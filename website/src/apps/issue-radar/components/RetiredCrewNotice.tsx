import { useTranslation } from 'react-i18next'

import StatusNotice from '../../../components/StatusNotice'
import { useIssueRadar } from '../context'

/** Says which crew this tab just retired, wherever the crews column lands next.
 *
 * Retiring drops the crew from the roster, and the context then re-points the
 * selection at the first crew left, or at the empty state. Without this the
 * person lands on a different crew's page with nothing saying the retire
 * happened, and could take that crew for the one they were looking at. Placed by
 * `Workspace` above both the crew page and the empty-state placeholder, so it
 * survives either landing. */
export default function RetiredCrewNotice() {
  const { t } = useTranslation()
  const { retiredCrewName, setRetiredCrew } = useIssueRadar()
  if (retiredCrewName === null) return null
  return (
    <div className="px-2 md:px-6 pt-4" aria-live="polite">
      <StatusNotice
        tone="info"
        message={t('apps.issueRadar.views.crews.page.retired_notice', { name: retiredCrewName })}
        onDismiss={() => { setRetiredCrew(null) }}
        testId="crew-retired-notice"
      />
    </div>
  )
}
