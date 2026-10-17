import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import DashboardPreviewLabel from './DashboardPreviewLabel'
import DashboardPreviewActions, { DashboardPreviewOutcomeNote, usePreviewHandover } from './DashboardPreviewActions'
import { MEMBER_DASHBOARD_QUERY_PREFIX, SESSION_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'
import { membersRosterQuery } from '../../api/membersQuery'
import { dashboardPreviewRef, onDashboardPreviewOpened, type DashboardPreviewTarget } from '../../utils/dashboardPreview'

const CrewDynamicDashboard = lazy(() => import('../members/CrewDynamicDashboard'))

/**
 * A STAGED dashboard, a crewmate's or a root session's, in the chat's side panel.
 *
 * The page `dashboard_preview` set aside, rendered by the same frame as the
 * crewmate's Dashboard tab with its `preview` read. Looking at it changes
 * nothing. A crewmate's page carries the person's Keep and Discard above the
 * frame, outside it; a session's page has no person route, so keeping it stays
 * the person's answer in chat, which the agent turns into `dashboard_apply`.
 *
 * Once the staged page is gone -- kept here, applied by the agent, discarded or
 * expired -- the panel hands over to the LIVE page with one line saying so,
 * rather than leaving a "closed" notice where the page was.
 *
 * The page's own reply options are not wired (`onAct` absent): a reply offered
 * by a page nobody has applied would act on a page that is not the crewmate's.
 *
 * No "open in browser" control: the preview URL is a JSON read, not a page, so
 * a browser tab would show data instead of the page. A modified click on the
 * chat link still reaches that URL.
 */
export default function DashboardPreviewPanel({ target }: { target: DashboardPreviewTarget }) {
  const { t } = useTranslation()
  // A crewmate's read is keyed by member NAME as well as slug. The roster names it:
  // a `member` target carries the slug and is matched to one name, a `member-name`
  // target (the `/members` page link) carries the exact name and is matched to one
  // slug, and neither is resolved by guessing, so the frame is withheld when no
  // single row matches. A session's page is keyed by its slot alone, so it needs
  // no roster.
  const { data: rows, isLoading, isError } = useQuery({ ...membersRosterQuery, enabled: target.kind !== 'session' })
  const matches = (rows ?? []).filter(r => (
    target.kind === 'member' ? r.slug === target.slug : target.kind === 'member-name' ? r.name === target.member : false
  ))
  const row = matches.length === 1 ? matches[0] : null
  const { outcome, leave, reset } = usePreviewHandover(
    target.kind === 'session' ? [SESSION_DASHBOARD_QUERY_PREFIX, target.slot] : [MEMBER_DASHBOARD_QUERY_PREFIX, row?.slug ?? ''],
  )
  // This panel stays mounted under one reference per crewmate, so a later preview
  // link for the same page lands here: go back to reading the staged page.
  const ref = dashboardPreviewRef(target)
  useEffect(() => onDashboardPreviewOpened(ref, reset), [ref, reset])
  const [stagedMs, setStagedMs] = useState(0)
  // Whoever ended the preview, the panel moves to the live page the same way.
  const onPreviewGone = useCallback((gone: boolean) => { if (gone) leave('closed') }, [leave])
  const showPreview = outcome === null
  const frameProps = showPreview
    ? { preview: true, onPreviewGone, onPreviewLoaded: setStagedMs }
    : { preview: false }
  const canAct = target.kind !== 'session' && !!row
  return (
    <div className="h-full min-h-0 flex flex-col" data-testid="dashboard-preview-panel">
      {showPreview && <DashboardPreviewLabel withActions={canAct} name={row?.name ?? ''} />}
      {showPreview && canAct && row && (
        <DashboardPreviewActions slug={row.slug} member={row.name} stagedMs={stagedMs} onDone={leave} />
      )}
      {outcome && (
        <DashboardPreviewOutcomeNote outcome={outcome} name={target.kind === 'session' ? t('commandCenter.title') : (row?.name ?? '')} />
      )}
      <div className="flex-1 min-h-0 overflow-auto">
        {target.kind === 'session' ? (
          <ErrorBoundary retryOnly>
            <Suspense fallback={null}>
              <CrewDynamicDashboard key={`${target.slot}:${showPreview ? 'preview' : 'live'}`} target={target} displayName={t('commandCenter.title')} {...frameProps} />
            </Suspense>
          </ErrorBoundary>
        ) : isError ? (
          // A failed roster read is not "no such crewmate", and a failed refresh is
          // reported even over a cached name. No agent hand-off: it navigates, and the
          // side panel's other tabs can hold unsaved file edits.
          <ErrorNotice className="m-3" message={t('pages.chat.dashboardPreviewPanel.roster_failed')} testId="dashboard-preview-roster-error" />
        ) : row ? (
          <ErrorBoundary retryOnly>
            <Suspense fallback={null}>
              <CrewDynamicDashboard key={`${row.slug}:${showPreview ? 'preview' : 'live'}`} target={{ kind: 'member', slug: row.slug, member: row.name }} displayName={row.name} {...frameProps} />
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
