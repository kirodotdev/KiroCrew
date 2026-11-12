import { lazy, Suspense } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { ContentSkeleton, PageHeader } from '../components/ui'
import ErrorBoundary from '../components/ErrorBoundary'
import RestartButton from '../components/RestartButton'
import McpTab from './overview/McpTab'
import { McpStagedEditsProvider } from './connections/mcpStagedEditsContext'
import { connectionCardHref } from './connections/connectionsLinks'
import type { McpPageView } from './settings/McpManagement'

/** Lazy: the sharing hero and its gateway status, servers and footprint
 *  queries are their own chunk. */
const McpManagement = lazy(() => import('./settings/McpManagement').then(m => ({ default: m.McpManagement })))

/** `?view=` picks the view, the convention Customize's Skills tab already reads.
 *  Servers is the default and is written as no param. */
const VIEW_PARAM = 'view'

/**
 * The MCP Servers page: the sharing hero and the approval card on top, then
 * the Servers view (the server list) and the Sharing view (each server's Share
 * switch, npm pre-install and the assessment).
 */
export default function McpPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const view: McpPageView = params.get(VIEW_PARAM) === 'sharing' ? 'sharing' : 'servers'
  // A selector, not a stack: switching views replaces the history entry.
  const setView = (next: McpPageView) => setParams(prev => {
    const updated = new URLSearchParams(prev)
    if (next === 'sharing') updated.set(VIEW_PARAM, next)
    else updated.delete(VIEW_PARAM)
    return updated
  }, { replace: true })

  return (
    <>
      <PageHeader
        title={t('pages.mcpPage.title')}
        // Apply & Restart lives here because this is where MCP server changes
        // are made: a newly enabled server reaches a chat that is already
        // running only after its session is relaunched. The muted line says
        // what restarts and what is kept BEFORE the click.
        actions={(
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 sm:justify-end">
            <span className="min-w-0 max-w-[34rem] text-[11.5px] leading-snug text-muted">{t('components.restartButton.beside_hint')}</span>
            <RestartButton />
          </div>
        )}
      />
      <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {/* The provider lets MCP sharing withhold its "Ask the agent" hand-off
            while the server list holds unapplied edits a /chat navigation
            would drop. */}
        <McpStagedEditsProvider>
          <ErrorBoundary>
            <Suspense fallback={<ContentSkeleton rows={6} />}>
              <McpManagement
                pageView={view}
                onPageViewChange={setView}
                serversList={<McpTab onManagedProviderClick={slug => navigate(connectionCardHref(slug))} />}
              />
            </Suspense>
          </ErrorBoundary>
        </McpStagedEditsProvider>
      </div>
    </>
  )
}
