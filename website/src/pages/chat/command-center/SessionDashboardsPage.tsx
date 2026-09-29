import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'
import { ArrowUpRight, LayoutDashboard } from 'lucide-react'
import { api } from '../../../api/client'
import { useAppSelector } from '../../../store'
import { Badge, Btn, Card, CardTitle, EmptyState, PageHeader, PanelSectionHeader, SearchInput } from '../../../components/ui'
import ErrorNotice from '../../../components/ErrorNotice'
import SimpleSelect from '../../../components/SimpleSelect'
import { fmtDateTime, fmtNumber } from '../../../i18n/format'
import { lastActivityEpoch } from '../sessionOrder'
import { missingSourcesNotice, useCommandCenter, type CommandCenterData } from './useCommandCenter'
import { runTitle, scopedSlots, slotKey, type RunNode } from './model'
import AttentionCard from './AttentionCard'
import TaskDashboardFrame from './TaskDashboardFrame'
import SessionStatusFrame from './SessionStatusFrame'
import AutomaticCardSetting from './AutomaticCardSetting'

/** One free summary read per visible card, sharing chat's websocket-invalidated cache. */
function SavedSummary({ slot, active }: { slot: string; active: boolean }) {
  const { t } = useTranslation()
  const summary = useQuery({ queryKey: ['session-summary', slot], queryFn: () => api.sessionSummary(slot),
    enabled: active, staleTime: 30_000, refetchOnWindowFocus: false, retry: false })
  const intents = summary.data?.enabled ? [...summary.data.intents].sort((a, b) => b.last_touched_turn - a.last_touched_turn) : []
  return <section className="space-y-2">
    <PanelSectionHeader label={t('pages.chat.sessionSummary.title')} />
    {/* No hand-off: sibling AttentionCards hold unsent answer drafts. */}
    <ErrorNotice message={summary.error ? t('pages.chat.sessionSummary.failed_title') : undefined} />
    {summary.isError && <Btn onClick={() => void summary.refetch()} disabled={summary.isFetching}>{t('pages.chat.sessionSummary.retry')}</Btn>}
    {summary.isPending ? <p role="status" className="text-sm text-muted">{t('pages.chat.sessionSummary.loading')}</p>
      : !intents.length && !summary.isError ? <p className="text-sm text-muted">{t(summary.data?.enabled === false ? 'pages.chat.sessionSummary.off_title' : 'pages.chat.sessionSummary.empty_title')}</p> : null}
    {summary.data?.generated_at != null && <p className="text-[11px] text-muted">{summary.data.stale
      ? t('pages.chat.sessionSummary.updated_behind', { when: fmtDateTime(summary.data.generated_at * 1000) })
      : `${t('pages.chat.sessionSummary.updated')} ${fmtDateTime(summary.data.generated_at * 1000)}`}</p>}
    {intents.map((intent, index) => <details key={`${intent.title}:${intent.origin_turn}`} open={index === 0} className="text-sm">
      <summary className="cursor-pointer font-medium break-words">{intent.title}</summary>
      <div className="space-y-1 pl-3 pt-1 text-muted">
        {intent.progress.length ? intent.progress.map((line, i) => <p key={i} className="break-words">{line}</p>) : <p className="break-words">{intent.initial_intent}</p>}
      </div>
    </details>)}
  </section>
}

function SessionDashboardCard({ node, data, active, team }: { node: RunNode; data: CommandCenterData; active: boolean; team: Set<string> }) {
  const { t } = useTranslation()
  const attention = data.attention.filter(item => item.slot === node.slot)
  const runs = data.nodes.filter(n => n.slot === node.slot && n.kind !== 'session')
  const blocked = runs.some(n => n.state === 'blocked')
  // A conductor's view is usually published by a builder it created, the same
  // team the task panel reads; showing only this slot's own said "no view".
  const dashboards = data.dashboards.filter(a => team.has(slotKey(a.session_key || '')))
  const [view, setView] = useState('')
  const selectedView = dashboards.find(a => a.slug === view) ?? dashboards[0]
  return <Card hidden={!active} data-testid="session-dashboard-card" data-slot={node.slot} className="min-w-0 self-start space-y-4">
    <div className="flex items-start gap-3">
      <div className="flex-1 min-w-0 space-y-1">
        <CardTitle className="break-words mb-0">{runTitle(node)}</CardTitle>
        <Badge variant={attention.length ? 'warn' : blocked ? 'err' : node.state === 'running' ? 'aim' : 'muted'}>
          {t(attention.length ? 'commandCenter.attention_filter' : blocked ? 'commandCenter.blocked' : node.state === 'running' ? 'commandCenter.running' : 'commandCenter.idle')}
        </Badge>
      </div>
      <Link to={`/chat?sid=${encodeURIComponent(node.slot)}`} className="text-accent text-[12px] inline-flex min-h-8 items-center gap-1 shrink-0">{t('commandCenter.open_session')}<ArrowUpRight size={13} /></Link>
    </div>
    {node.detail && <p className="text-sm text-muted break-words">{node.detail}</p>}
    <SessionStatusFrame slot={node.slot} title={runTitle(node)} active={active} />
    <SavedSummary slot={node.slot} active={active} />
    {runs.filter(n => n.error).map(n => <div key={n.id}>
      <p className="text-sm font-medium">{runTitle(n)}</p>
      {/* No hand-off: this session's unsent answers stay in the cards above. */}
      <ErrorNotice message={n.error} />
    </div>)}
    {dashboards.length > 1 && <SimpleSelect aria-label={t('commandCenter.published_view')} options={dashboards.map(a => a.slug)} optionLabels={dashboards.map(a => a.name)} value={selectedView.slug} onChange={setView} />}
    {selectedView ? <TaskDashboardFrame key={selectedView.slug} artifact={selectedView} active={active} />
      : <p className="text-sm text-muted border-t border-border pt-3">{t('commandCenter.no_dashboard')}</p>}
  </Card>
}

/** An explicit fleet surface. Task panels never widen to this scope by accident. */
export default function SessionDashboardsPage() {
  const { t } = useTranslation()
  const data = useCommandCenter(null, true, 'fleet')
  const slots = useAppSelector(s => s.dashboard.slots)
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  const [query, setQuery] = useState('')
  const [attentionOnly, setAttentionOnly] = useState(false)
  const [limit, setLimit] = useState(12)
  const attention = new Set(data.attention.map(item => item.slot))
  const blocked = new Set(data.nodes.filter(n => n.state === 'blocked').map(n => n.slot))
  const recency = new Map(slots.map(slot => [slot.key, lastActivityEpoch(slot)]))
  const priority = (node: RunNode) => attention.has(node.slot) ? 0 : blocked.has(node.slot) ? 1 : node.state === 'running' ? 2 : 3
  const sessionNodes = data.nodes.filter(n => n.kind === 'session')
  const sorted = [...sessionNodes].sort((a, b) => priority(a) - priority(b) || (recency.get(b.slot) || 0) - (recency.get(a.slot) || 0))
  // Order is taken when the set, what needs attention, the filter or the page
  // changes, not on every activity update: moving a card's DOM node reloads its
  // iframes, and their single-use documents then 404. DOM order stays reading order.
  const orderKey = JSON.stringify([sessionNodes.map(n => n.slot).sort(), [...attention].sort(), [...blocked].sort(), query, attentionOnly, limit])
  const [frozen, setFrozen] = useState<{ key: string; order: string[] }>({ key: '', order: [] })
  if (frozen.key !== orderKey) setFrozen({ key: orderKey, order: sorted.map(n => n.slot) })
  const order = frozen.key === orderKey ? frozen.order : sorted.map(n => n.slot)
  const bySlot = new Map(sessionNodes.map(n => [n.slot, n]))
  const nodes = order.map(slot => bySlot.get(slot)).filter((n): n is RunNode => !!n)
  const matching = nodes.filter(n => (!attentionOnly || attention.has(n.slot)) && `${runTitle(n)} ${n.slot}`.toLowerCase().includes(query.trim().toLowerCase()))
  const pageEnd = Math.min(limit, Math.max(12, Math.ceil(matching.length / 12) * 12))
  const visible = new Set(matching.slice(pageEnd - 12, pageEnd).map(n => n.slot))
  const matchingSlots = new Set(matching.map(n => n.slot))
  const sessionBySlot = new Map(nodes.map(n => [n.slot, n]))
  const pending = data.attention.filter(item => matchingSlots.has(item.slot))
  return <>
    <PageHeader title={t('commandCenter.all_title')} subtitle={t('commandCenter.all_description')} />
    <div className="px-4 md:px-6 pb-8 overflow-y-auto flex-1 min-h-0 space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <SearchInput className="flex-1 min-w-0" placeholder={t('pages.sessionsPage.search_placeholder')} value={query} onChange={e => { setQuery(e.target.value); setLimit(12) }} />
        <Btn primary={attentionOnly} aria-pressed={attentionOnly} onClick={() => { setAttentionOnly(!attentionOnly); setLimit(12) }}>{t('commandCenter.attention_filter')} ({fmtNumber(pending.length)})</Btn>
      </div>
      {/* No hand-off: filtered-out inbox items retain their QuestionCard answer drafts. */}
      {data.stale && <ErrorNotice message={t('commandCenter.stale')} />}
      {/* No hand-off: the attention cards here can hold unsent QuestionCard answer drafts. */}
      <ErrorNotice message={missingSourcesNotice(data.missing)} />
      {(!slotsLoaded || data.loading) && <p role="status" className="text-sm text-muted">{t('commandCenter.loading')}</p>}
      <section aria-label={t('commandCenter.attention_filter')} className="space-y-3">
        <PanelSectionHeader label={t('commandCenter.attention_filter')} count={pending.length} />
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-3 items-start">
          {/* Keep one control per request mounted through filters. The summary
              page limit must never hide a decision waiting for its owner. */}
          {data.attention.map(item => {
            const node = sessionBySlot.get(item.slot)!
            return <div key={item.id} hidden={!matchingSlots.has(item.slot)} className="min-w-0">
              <AttentionCard item={item} title={runTitle(node)} context={node.detail} onDraftChange={item.question ? active => data.onQuestionDraftChange(item.question!, active) : undefined} />
            </div>
          })}
        </div>
        {slotsLoaded && !data.loading && !data.stale && !pending.length && <p className="text-sm text-muted">{t('commandCenter.no_input')}</p>}
      </section>
      <AutomaticCardSetting active={slotsLoaded} />
      <PanelSectionHeader label={t('pages.sessionsPage.page_title')} count={matching.length} />
      <div className="grid grid-cols-1 xl:grid-cols-2 gap-4 items-start">
        {/* Preserve selection, but unmount inactive iframe documents to cap resources. */}
        {nodes.map(node => <SessionDashboardCard key={node.slot} node={node} data={data} active={visible.has(node.slot)} team={new Set(scopedSlots(slots, node.slot).map(s => s.key))} />)}
      </div>
      {slotsLoaded && !data.loading && !matching.length && <EmptyState icon={<LayoutDashboard size={24} />} title={t(nodes.length ? 'commandCenter.no_matches' : 'pages.sessionsPage.empty_title')} />}
      <div className="flex flex-wrap gap-2">
        {pageEnd > 12 && <Btn onClick={() => setLimit(pageEnd - 12)}>{t('commandCenter.previous_sessions')}</Btn>}
        {matching.length > pageEnd && <Btn onClick={() => setLimit(pageEnd + 12)}>{t('commandCenter.next_sessions')}</Btn>}
      </div>
    </div>
  </>
}
