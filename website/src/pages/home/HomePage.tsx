/**
 * Home — mission control for the crew.
 *
 * One page above Sessions in the rail that answers, without reading the rail's
 * badges one by one: what is running, what is waiting on a person, what is
 * scheduled, and what today has cost. Every number is a source another page
 * already reads (the command center's fleet model, the schedule, the bot roster,
 * the provider's usage report), so Home adds no polling of its own and cannot
 * disagree with the page it links to.
 *
 * Layout: a stat strip, then the agent board (what each agent is doing) on the
 * left with the Needs-you panel, the upcoming jobs and the bots stacked on the
 * right, so the work and the decisions it waits on sit side by side.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowUpRight } from 'lucide-react'
import { cronJobsQuery } from '../../api/cronJobsQuery'
import { PageHeader, StatCard } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import GatewayHealthLine from '../../components/GatewayHealthLine'
import { fmtCompact, fmtList, fmtNumber, fmtRelative } from '../../i18n/format'
import { formatCost } from '../../utils/formatCost'
import { useAppSelector } from '../../store'
import { missingSourcesNotice, useCommandCenter } from '../chat/command-center/useCommandCenter'
import { useTodayUsage } from '../overview/todayUsage'
import { reportsDollars } from '../../api/acpBackend'
import { activeCounts, fleetRowExtras, scheduleSummary, splitAttention, type FleetRowExtras } from './homeModel'
import HomeNeedsYou from './HomeNeedsYou'
import HomeFleetList from './HomeFleetList'
import HomeBots from './HomeBots'
import HomeSchedule from './HomeSchedule'
import HomeSpend from './HomeSpend'

export default function HomePage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const fleet = useCommandCenter(null, true, 'fleet')
  const slots = useAppSelector(s => s.dashboard.slots)
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  // The shared job list: the websocket's `refresh` frame keeps it current, so a
  // schedule edit anywhere reaches this tile without a poll of its own.
  const crons = useQuery(cronJobsQuery)
  const usage = useTodayUsage()

  const active = activeCounts(fleet.nodes)
  const activeDetail = fmtList([
    t('pages.overviewPage.today_sessions', { count: active.sessions, amount: fmtNumber(active.sessions) }),
    t('pages.homePage.subagents', { count: active.subagents, amount: fmtNumber(active.subagents) }),
    t('pages.homePage.workflows', { count: active.workflows, amount: fmtNumber(active.workflows) }),
  ])

  const attention = useMemo(() => splitAttention(fleet.attention), [fleet.attention])
  const attentionDetail = [
    attention.blockers.length
      ? fmtList([
        t('pages.homePage.questions_count', { count: attention.questions, amount: fmtNumber(attention.questions) }),
        t('pages.homePage.approvals_count', { count: attention.approvals, amount: fmtNumber(attention.approvals) }),
      ])
      : t('commandCenter.no_input'),
    attention.suggestions.length ? t('pages.homePage.suggestions_count', { count: attention.suggestions.length, amount: fmtNumber(attention.suggestions.length) }) : '',
  ].filter(Boolean).join(' · ')

  const schedule = crons.data ? scheduleSummary(crons.data) : undefined
  const scheduleDetail = schedule && [
    schedule.next?.next_run_ts != null
      ? t('pages.homePage.next_run', { name: schedule.next.name, when: fmtRelative(schedule.next.next_run_ts * 1000) })
      : t('pages.homePage.no_next_run'),
    schedule.failing ? t('pages.homePage.failing_count', { count: schedule.failing, amount: fmtNumber(schedule.failing) }) : '',
    schedule.paused ? t('pages.homePage.paused_count', { count: schedule.paused, amount: fmtNumber(schedule.paused) }) : '',
  ].filter(Boolean).join(' · ')

  // Today's tile leads with cost when the harness bills in dollars; otherwise the
  // day's session count, which every harness reports. Which one it is waits for the
  // harness read, so the slot never shows one meaning and then flips to the other.
  const billing = usage.provider.capabilities.usageBilling
  const dollars = reportsDollars(usage.cfgQuery.data)
  const todayValue = !billing ? '—'
    : usage.usage.isError ? '—'
      : !usage.usage.data || usage.cfgQuery.isPending ? undefined
        : dollars ? formatCost(usage.costUsd ?? 0) : fmtCompact(usage.today?.sessions ?? 0)
  const todayDetail = billing ? usage.line : t('pages.overview.usageTab.usage_tracking_is_not_available_for', { provider: usage.provider.displayName })

  // The counts show once the slot list and both decision reads (questions,
  // approvals) have answered. An optional source still loading (workflows, the
  // work board) must not hold the whole page on skeletons; `missing` names it.
  const ready = slotsLoaded && fleet.updatedAt > 0
  const extras = useMemo(() => new Map<string, FleetRowExtras>(slots.map(s => [s.key, fleetRowExtras(s)])), [slots])
  const attentionSlots = useMemo(() => new Set(attention.blockers.map(a => a.slot)), [attention.blockers])

  return <>
    <PageHeader title={t('pages.homePage.title')} subtitle={t('pages.homePage.subtitle')} />
    <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0 space-y-4">
      {/* One alarm for one fault: a lost gateway connection outranks a missing
          optional source, and the schedule read failing is said on its own line. */}
      {/* No hand-off on either notice: the Needs-you cards below hold unsent answer drafts. */}
      <ErrorNotice message={fleet.stale ? t('commandCenter.stale') : missingSourcesNotice(fleet.missing)} />
      <ErrorNotice message={crons.isError ? t('pages.homePage.schedule_unavailable') : undefined} testId="home-schedule-error" />
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3" data-testid="home-stat-strip">
        <StatCard
          data-testid="home-stat-active"
          label={t('pages.homePage.active_now')}
          value={ready ? fmtNumber(active.total) : undefined}
          detail={activeDetail}
          accent={active.total > 0}
          onClick={() => navigate('/session-dashboards')}
        />
        <StatCard
          data-testid="home-stat-attention"
          label={t('commandCenter.attention_filter')}
          value={ready ? fmtNumber(attention.blockers.length) : undefined}
          detail={attentionDetail}
          colorClass={attention.blockers.length ? 'text-warn-text' : undefined}
        />
        <StatCard
          data-testid="home-stat-schedule"
          label={t('pages.homePage.scheduled')}
          value={schedule ? fmtNumber(schedule.enabled) : crons.isError ? '—' : undefined}
          detail={scheduleDetail}
          colorClass={schedule?.failing ? 'text-danger-text' : undefined}
          onClick={() => navigate('/schedule')}
        />
        <StatCard
          data-testid="home-stat-today"
          label={t('pages.homePage.today')}
          value={todayValue}
          detail={todayDetail}
          onClick={() => navigate('/settings/overview?view=usage')}
        />
      </div>
      {/* The board and, under it, every decision waiting on a person as a grid of
          cards; beside them where today's usage went, what is coming up and who
          is working. */}
      <div className="grid grid-cols-1 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)] gap-4 items-start">
        <div className="space-y-4 min-w-0">
          <HomeFleetList nodes={fleet.nodes} attentionSlots={attentionSlots} extras={extras} ready={ready} />
          <HomeNeedsYou data={fleet} attention={attention} ready={ready} />
        </div>
        <div className="space-y-4 min-w-0">
          <HomeSpend dollars={dollars} />
          <HomeSchedule jobs={crons.data} />
          <HomeBots />
        </div>
      </div>
      <footer className="flex flex-wrap items-center justify-between gap-2 pt-2 border-t border-border">
        <GatewayHealthLine compact />
        <Link to="/settings/overview" className="text-accent text-[12px] inline-flex items-center gap-1">{t('pages.homePage.open_overview')}<ArrowUpRight size={13} /></Link>
      </footer>
    </div>
  </>
}
