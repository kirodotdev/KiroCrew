import { useState } from 'react'
import { LifeBuoy, Flag } from 'lucide-react'
import { Card, CardTitle, Btn } from '../../components/ui'
import ReportProblemModal from '../../components/ReportProblemModal'

import { i18nT } from '../../i18n/t'

/**
 * Settings › About › "Report a Problem".
 *
 * Thin entry point: the whole flow lives in the shared
 * :file:`components/ReportProblemModal.tsx`, which the nav rail's "Report
 * issue" link mounts too — this card only owns the Support section's own copy
 * and the open/close state.
 */
export default function ReportProblemCard() {
  const [open, setOpen] = useState(false)

  return (
    <>
      <Card>
        <CardTitle>
          <LifeBuoy size={15} className="lucide-inline" />{' '}
          {i18nT('pages.settings.reportProblemCard.support')}
        </CardTitle>
        <div className="flex items-center justify-between gap-4 py-1.5">
          <span className="text-[13px] text-muted">
            {i18nT('pages.settings.reportProblemCard.blurb')}
          </span>
          <Btn onClick={() => setOpen(true)}>
            <Flag size={13} className="lucide-inline" />{' '}
            {i18nT('pages.settings.reportProblemCard.report_a_problem')}
          </Btn>
        </div>
      </Card>

      <ReportProblemModal open={open} onClose={() => setOpen(false)} />
    </>
  )
}
