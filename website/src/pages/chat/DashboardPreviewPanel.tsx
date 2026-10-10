import { lazy, Suspense, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import { Badge } from '../../components/ui'
import { membersRosterQuery } from '../../api/membersQuery'

const CrewDynamicDashboard = lazy(() => import('../members/CrewDynamicDashboard'))

/**
 * A crewmate's STAGED dashboard, in the chat's side panel.
 *
 * The page `dashboard_preview` set aside, rendered by the same frame as the
 * crewmate's Dashboard tab with its `preview` read. Looking at it changes
 * nothing: there is no Apply control here. Keeping the page is the person's
 * answer in the chat, which the agent turns into `dashboard_apply`; this panel
 * only says plainly that what it shows is not applied.
 *
 * The page's own reply options are not wired (`onAct` absent): a reply offered
 * by a page nobody has applied would act on a page that is not the crewmate's.
 *
 * No "open in browser" control: the preview URL is a JSON read, not a page, so
 * a browser tab would show data instead of the page. A modified click on the
 * chat link still reaches that URL.
 */
export default function DashboardPreviewPanel({ slug }: { slug: string }) {
  const { t } = useTranslation()
  // The read is keyed by member NAME as well as slug. The roster names it; a slug
  // two names share is not resolved by guessing, so the frame is withheld.
  const { data: rows, isLoading, isError } = useQuery(membersRosterQuery)
  const matches = (rows ?? []).filter(r => r.slug === slug)
  const member = matches.length === 1 ? matches[0].name : null
  // Once the staged page is applied or expires, "not applied" would be false.
  const [gone, setGone] = useState(false)
  return (
    <div className="h-full min-h-0 flex flex-col" data-testid="dashboard-preview-panel">
      {!gone && (
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 px-3 py-2 border-b border-border">
          <Badge variant="warn" data-testid="dashboard-preview-badge">{t('pages.chat.dashboardPreviewPanel.badge')}</Badge>
          <span className="text-xs text-muted min-w-0 basis-full">{t('pages.chat.dashboardPreviewPanel.hint')}</span>
        </div>
      )}
      <div className="flex-1 min-h-0 overflow-auto">
        {isError ? (
          // A failed roster read is not "no such crewmate", and a failed refresh is
          // reported even over a cached name. No agent hand-off: it navigates, and the
          // side panel's other tabs can hold unsaved file edits.
          <ErrorNotice className="m-3" message={t('pages.chat.dashboardPreviewPanel.roster_failed')} testId="dashboard-preview-roster-error" />
        ) : member ? (
          <ErrorBoundary retryOnly>
            <Suspense fallback={null}>
              <CrewDynamicDashboard key={slug} slug={slug} member={member} displayName={member} preview onPreviewGone={setGone} />
            </Suspense>
          </ErrorBoundary>
        ) : isLoading ? null : (
          <p className="p-4 text-sm text-muted" data-testid="dashboard-preview-no-member">
            {t('pages.chat.dashboardPreviewPanel.no_member')}
          </p>
        )}
      </div>
    </div>
  )
}
