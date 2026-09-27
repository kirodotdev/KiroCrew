import { useMemo } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { RotateCw } from 'lucide-react'
import { api } from '../../../api/client'
import type { Artifact } from '../../../types'
import { useTheme } from '../../../hooks/useTheme'
import { useSandboxDoc } from '../../../hooks/useSandboxDoc'
import { readThemeVars } from '../../../lib/widgetSrcdoc'
import { dashboardDocument } from './dashboardDocument'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { i18nT } from '../../../i18n/t'

/** A presentation surface, deliberately NOT an agent-control bridge. Model code
 * can design any layout, but cannot read the host or submit/approve on its behalf. */
export const TASK_DASHBOARD_SANDBOX = ''

export default function TaskDashboardFrame({ artifact, active }: { artifact: Artifact; active: boolean }) {
  const { theme, colorTheme, themeVersion } = useTheme()
  // The theme provider changes document CSS variables, including in-place edits.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const themeVars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const content = useQuery({
    queryKey: ['command-center', 'artifact', artifact.slug, artifact.version, artifact.updated_at],
    queryFn: () => api.artifact(artifact.slug) as Promise<Artifact>,
    enabled: active, staleTime: Infinity, placeholderData: keepPreviousData,
  })
  const srcdoc = useMemo(() => active && content.data?.content ? dashboardDocument(content.data.content, themeVars, theme)
    : null, [active, content.data?.content, theme, themeVars])
  const document = useSandboxDoc(srcdoc)
  return <div data-testid="task-dashboard-frame" className="flex min-h-80 flex-1 flex-col border border-border rounded-lg overflow-hidden bg-bg">
    {/* No hand-off: the panel keeps pending QuestionCard answer drafts mounted. */}
    {(content.isError || document.failed) && <ErrorNotice message={i18nT('commandCenter.dashboard_error')} />}
    <div className="flex items-center justify-between gap-2 px-3 py-2 border-b border-border">
      <span className="text-[11px] text-muted">{i18nT('commandCenter.model_designed')}</span>
      <Btn disabled={content.isFetching || document.pending} aria-label={i18nT('commandCenter.refresh')} onClick={() => { void content.refetch(); document.retry() }}><RotateCw size={13} /></Btn>
    </div>
    {!document.url && <p role="status" className="p-4 text-sm text-muted">{i18nT('commandCenter.loading')}</p>}
    {active && document.url && <iframe title={artifact.name} src={document.url} sandbox={TASK_DASHBOARD_SANDBOX}
      referrerPolicy="no-referrer" className="w-full flex-1 min-h-80 border-0" style={{ transform: 'translateZ(0)' }} />}
  </div>
}
