import ErrorNotice from '../../../components/ErrorNotice'
import { useIssueRadar } from '../context'
import { gitlabGroupErrorText, useGitlabGroups } from '../lib/gitlabGroups'
import { DASHBOARDS } from '../views/registry'

import { i18nT } from '../../../i18n/t'

/** Body of the "Dashboards" accordion section: a nav list (Overview, Tagging…)
 * driven entirely by the registry. Selecting one opens
 * that dashboard in the main area. */
/** `onNavigate` fires after any navigation so a narrow viewport can collapse the
 * full-width rail — otherwise the tap navigates behind a rail still covering it. */
export default function DashboardsSection({ onNavigate }: { onNavigate?: () => void }) {
  const { mainView, dashboardTab, openDashboard } = useIssueRadar()
  // "Review requests" lists followed GitLab groups, so it is offered only once
  // one is followed, or while it is the page on screen. The read is local and
  // never calls GitLab. A failed read is not "no groups": the entry stays
  // listed, since a followed group may be behind the failure, and the failure
  // is shown below the list.
  const groups = useGitlabGroups()
  const followsAGroup = groups.isError || (groups.data?.length ?? 0) > 0
  const onReviews = mainView === 'dashboard' && dashboardTab === 'reviews'
  const dashboards = DASHBOARDS.filter((d) => d.key !== 'reviews' || followsAGroup || onReviews)

  return (
    <div className="px-3 pt-1">
      <div className="flex flex-col gap-0.5">
        {dashboards.map((d) => {
          const isActive = mainView === 'dashboard' && dashboardTab === d.key
          return (
            <button
              key={d.key}
              onClick={() => { openDashboard(d.key); onNavigate?.() }}
              className={`w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-[13px] text-left cursor-pointer transition-colors ${
                isActive ? 'bg-accent-subtle text-text font-medium' : 'text-muted hover:bg-bg-hover'
              }`}
            >
              <d.icon size={14} className={`flex-shrink-0 ${isActive ? 'text-accent' : ''}`} />
              <span className="flex-1">{d.label}</span>
            </button>
          )
        })}
      </div>
      {groups.isError && (
        <ErrorNotice
          message={i18nT('apps.issueRadar.views.settings.gitlabGroups.list_error', { error: gitlabGroupErrorText(groups.error) })}
          askAgent
          actionPlacement="below"
          className="mt-2"
          testId="dashboards-gitlab-groups-error"
        />
      )}
    </div>
  )
}
