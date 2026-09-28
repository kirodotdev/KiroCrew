import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { api } from '../../../api/client'
import { useTheme } from '../../../hooks/useTheme'
import { useSandboxDoc } from '../../../hooks/useSandboxDoc'
import { readThemeVars } from '../../../lib/widgetSrcdoc'
import { fmtDateTime } from '../../../i18n/format'
import { Btn } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import { dashboardDocument } from './dashboardDocument'
import { TASK_DASHBOARD_SANDBOX } from './TaskDashboardFrame'
import type { DynamicDashboardCard } from '../../../types/dynamicDashboard'
import { useAppSelector } from '../../../store'
import { isPrivateMemoryMode } from '../../../utils/sessionRefs'

const STATUS_KEYS: Record<Exclude<DynamicDashboardCard['status'], 'published'>, string> = {
  disabled: 'commandCenter.card_disabled', waiting: 'commandCenter.card_waiting',
  queued: 'commandCenter.card_queued', generating: 'commandCenter.card_generating',
  budget: 'commandCenter.card_budget', failed: 'commandCenter.card_failed',
  unavailable: 'commandCenter.card_unavailable',
}

/** The host owns freshness and controls. The isolated document owns presentation. */
export default function SessionStatusFrame({ slot, title, active }: { slot: string; title: string; active: boolean }) {
  const { t } = useTranslation()
  const { theme, colorTheme, themeVersion } = useTheme()
  const owner = useAppSelector(s => s.dashboard.slots.find(item => item.key === slot))
  // Mirrors the producer: team workers (a creator link) get no automatic card.
  const eligible = !isPrivateMemoryMode(owner?.memory_mode) && owner?.executor !== 'remote' && !owner?.created_by
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const vars = useMemo(() => readThemeVars(), [theme, colorTheme, themeVersion])
  const query = useQuery({ queryKey: ['dashboard-card', slot, owner?.linked_session_key ?? '', owner?.memory_mode ?? 'persistent'], queryFn: () => api.dashboardCard(slot),
    enabled: active && eligible, staleTime: Infinity, gcTime: 60_000, retry: false, refetchOnWindowFocus: false })
  const card = eligible ? query.data?.card : null
  const html = useMemo(() => active && card ? dashboardDocument(card.html, vars, theme, card.data) : null,
    [active, card, vars, theme])
  const document = useSandboxDoc(html)
  const status = eligible ? query.data?.status ?? 'waiting' : 'unavailable'
  return <section data-testid="session-status-frame" className="space-y-2 min-w-0">
    {eligible && query.data?.published_at != null && <p className="text-[11px] text-muted">{t('commandCenter.card_content_published', { time: fmtDateTime(query.data.published_at * 1000) })}</p>}
    {status !== 'published' && status !== 'failed' && <p role="status" className="text-[12px] text-muted">{t(STATUS_KEYS[status])}</p>}
    {query.data?.stale && card && <p className="text-[12px] text-muted">{t('commandCenter.card_stale')}</p>}
    {/* No hand-off: native decision cards above may hold unsent answers. */}
    <ErrorNotice message={query.isError || document.failed ? t('commandCenter.dashboard_error') : status === 'failed' ? t('commandCenter.card_failed') : undefined} />
    {(query.isError || document.failed) && <Btn onClick={() => { void query.refetch(); document.retry() }} disabled={query.isFetching || document.pending}>{t('commandCenter.refresh')}</Btn>}
    {active && document.url && <iframe title={title} src={document.url} sandbox={TASK_DASHBOARD_SANDBOX} referrerPolicy="no-referrer"
      className="w-full min-h-64 border border-border rounded-lg bg-bg" />}
  </section>
}
