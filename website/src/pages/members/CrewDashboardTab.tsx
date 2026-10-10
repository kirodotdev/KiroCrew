import { lazy, Suspense } from 'react'
import { useTranslation } from 'react-i18next'
import ErrorBoundary from '../../components/ErrorBoundary'

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
export default function CrewDashboardTab({ slug, member, displayName, onAct, preview = false, onExitPreview }: {
  slug: string
  member: string
  displayName: string
  /** Put a reply the page offered into this crewmate's chat box. */
  onAct?: (text: string) => void
  /** Show the STAGED page under a preview band instead of the live one. */
  preview?: boolean
  /** Return the tab to the live page. */
  onExitPreview?: () => void
}) {
  const { t } = useTranslation()
  return (
    <div className="h-full min-h-0 flex flex-col" data-testid="crew-dashboard-tab">
      <ErrorBoundary>
        <Suspense fallback={<p role="status" className="text-sm text-muted">{t('pages.membersPage.webview_rendering')}</p>}>
          {/* Keyed on the slug: a different crewmate is a different instance, a
              different manifest and a different minted document, and the held
              "last good page" must not survive the switch -- one crewmate's
              numbers under another's name is the one thing this surface must
              never show. Keyed on `preview` for the same reason: the staged page
              and the live one are two documents, and the held page of one must
              never be drawn under the other's band. */}
          <CrewDynamicDashboard
            key={`${slug}:${preview ? 'preview' : 'live'}`}
            slug={slug}
            member={member}
            displayName={displayName || member}
            onAct={onAct}
            preview={preview}
            onExitPreview={onExitPreview}
          />
        </Suspense>
      </ErrorBoundary>
    </div>
  )
}
