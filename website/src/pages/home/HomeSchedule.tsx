/**
 * Home's upcoming schedule: the next few jobs due, soonest first, each opening its
 * detail on the Schedule page. Reads the same shared job list as the stat strip.
 */
import { useTranslation } from 'react-i18next'
import { CalendarDays } from 'lucide-react'
import type { CronJob } from '../../types'
import { Badge, Card, EmptyState, PanelSectionHeader } from '../../components/ui'
import { fmtRelative } from '../../i18n/format'
import { upcomingJobs } from './homeModel'
import { ListRow, RowLink } from './homeRows'

/** How many upcoming jobs the section lists; the Schedule page has the rest. */
const UPCOMING_LIMIT = 5

/** `jobs` is undefined while the list loads or when its read failed; the page's
 *  own notice says so, and this card stays quiet rather than saying it twice. */
export default function HomeSchedule({ jobs }: { jobs: readonly CronJob[] | undefined }) {
  const { t } = useTranslation()
  const upcoming = jobs ? upcomingJobs(jobs, UPCOMING_LIMIT) : []
  return <section aria-label={t('nav.schedule')} className="space-y-3" data-testid="home-schedule">
    <PanelSectionHeader label={t('pages.homePage.upcoming')} count={jobs ? upcoming.length : undefined}
      trailing={<RowLink to="/schedule">{t('pages.homePage.open_page')}</RowLink>} />
    <Card className="py-1">
      {jobs && !upcoming.length && <EmptyState icon={<CalendarDays size={24} />} title={t('pages.homePage.no_next_run')} testId="home-schedule-empty" />}
      {upcoming.length > 0 && <ul className="list-none m-0 p-0">
        {upcoming.map(job => (
          <ListRow key={job.id} data-testid="home-schedule-row">
            <div className="flex-1 min-w-0">
              <div className="text-sm font-medium truncate">{job.name}</div>
              <div className="text-[12px] text-muted truncate">{t('pages.homePage.due', { when: fmtRelative(job.next_run_ts * 1000) })}</div>
            </div>
            {/* The Schedule page's own status words, so a job reads the same here and there. */}
            {job.is_running ? <Badge variant="ok">{t('pages.schedulePage.running')}</Badge>
              : job.last_status === 'error' ? <Badge variant="err">{t('pages.schedulePage.error')}</Badge> : null}
            <RowLink to={`/schedule?job=${encodeURIComponent(job.id)}`}>{t('pages.homePage.open')}</RowLink>
          </ListRow>
        ))}
      </ul>}
    </Card>
  </section>
}
