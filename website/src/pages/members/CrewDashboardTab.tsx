import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import ErrorBoundary from '../../components/ErrorBoundary'
import DashboardPreviewLabel from '../chat/DashboardPreviewLabel'
import DashboardPreviewActions, { DashboardPreviewOutcomeNote, usePreviewHandover } from '../chat/DashboardPreviewActions'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'

const CrewDynamicDashboard = lazy(() => import('./CrewDynamicDashboard'))

/** The crewmate's Dashboard tab: the crewmate's DYNAMIC DASHBOARD and nothing else.
 *
 * The page is the crewmate's own copy of a template, whose every number comes
 * either from a crew-log fold the gateway read or from an agentic value the
 * crewmate wrote and the host type-checked against the template's manifest. The
 * page runs its own JS in the crew webview sandbox, so it can draw a chart; it
 * cannot invent a number, because it has no channel that produces one.
 *
 * The tab holds the dashboard alone and offers no view switch. The raw crew-log
 * record is a Developer Mode surface, reached from the chat panel's own Crew log
 * view, so the thread's slot is not part of this tab's input. */
export default function CrewDashboardTab({ slug, member, displayName, avatar, onAct, onOpenProfileTab, preview = false, onLeavePreview }: {
  slug: string
  member: string
  displayName: string
  /** The crew record's `avatar` field, verbatim: the suggestions page wears the roster row's face. */
  avatar?: unknown
  /** Put a reply the page offered into this crewmate's chat box. */
  onAct?: (text: string) => void
  /** Called when the person clicks the "Layout locked · Profile" badge to go
   *  straight to the Profile tab where the lock switch lives. */
  onOpenProfileTab?: () => void
  /** Show the STAGED page under the shared preview label instead of the live one. */
  preview?: boolean
  /** The staged page is no longer staged (kept, discarded, applied by the agent or
   *  expired): stop asking for it, so the tab shows the live page. */
  onLeavePreview?: () => void
}) {
  const { t } = useTranslation()
  const name = displayName || member
  const { outcome, leave, reset } = usePreviewHandover([MEMBER_DASHBOARD_QUERY_PREFIX, slug], onLeavePreview)
  const [stagedMs, setStagedMs] = useState(0)
  // A later preview link starts on that page again, with no note from the last one.
  useEffect(() => {
    if (preview) reset()
  }, [preview, reset])
  // Whoever ended the preview -- this tab's Keep, the agent's `dashboard_apply`, a
  // discard or the clock -- the tab moves to the live page the same way.
  const onPreviewGone = useCallback((gone: boolean) => { if (gone) leave('closed') }, [leave])
  const showPreview = preview && outcome === null
  return (
    <div className="h-full min-h-0 flex flex-col" data-testid="crew-dashboard-tab">
      {showPreview && <DashboardPreviewLabel withActions name={name} />}
      {showPreview && (
        <DashboardPreviewActions slug={slug} member={member} stagedMs={stagedMs} onDone={leave} />
      )}
      {!showPreview && outcome && <DashboardPreviewOutcomeNote outcome={outcome} name={name} />}
      <ErrorBoundary>
        <Suspense fallback={<p role="status" className="text-sm text-muted">{t('pages.membersPage.webview_rendering')}</p>}>
          {/* Keyed on the slug: a different crewmate is a different instance, a
              different manifest and a different minted document, and the held
              "last good page" must not survive the switch -- one crewmate's
              numbers under another's name is the one thing this surface must
              never show. Keyed on `preview` for the same reason: the staged page
              and the live one are two documents, and the held page of one must
              never be drawn under the other's label. */}
          <CrewDynamicDashboard
            key={`${slug}:${showPreview ? 'preview' : 'live'}`}
            target={{ kind: 'member', slug, member }}
            displayName={name}
            avatar={avatar}
            onAct={onAct}
            onOpenProfileTab={onOpenProfileTab}
            preview={showPreview}
            onPreviewGone={showPreview ? onPreviewGone : undefined}
            onPreviewLoaded={showPreview ? setStagedMs : undefined}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  )
}
