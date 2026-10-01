import { useState, type ReactNode } from 'react'
import { useMutation } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { LayoutDashboard, MessageSquare, ShieldCheck } from 'lucide-react'
import { PanelSectionHeader, Btn } from '../../../components/ui'
import SegmentedControl from '../../../components/SegmentedControl'
import SimpleSelect from '../../../components/SimpleSelect'
import ErrorNotice from '../../../components/ErrorNotice'
import InfoTip from '../../../components/InfoTip'
import { fmtDateTime } from '../../../i18n/format'
import { missingSourcesNotice, useCommandCenter } from './useCommandCenter'
import TaskDashboardFrame from './TaskDashboardFrame'
import SessionStatusFrame from './SessionStatusFrame'
import AutomaticCardSetting from './AutomaticCardSetting'
import AttentionCard from './AttentionCard'
import StatusTiles from './StatusTiles'
import TileList from './TileList'
import { APPROVAL_MODE_KEYS, runTitle, type RunState } from './model'
import { PANEL_HEADING_ATTR } from './panelHeading'
import { sendTurn } from '../../../chat-core/transport/sendTurn'
import { REQUEST_PUBLISHED_VIEW } from './commandCenter.prompt'

const STATE_KEYS: Record<RunState, string> = {
  running: 'commandCenter.running', idle: 'commandCenter.idle', done: 'commandCenter.done',
  blocked: 'commandCenter.blocked', waiting: 'commandCenter.waiting', needs_input: 'commandCenter.state_needs_input', stopped: 'commandCenter.stopped',
}

export default function CommandCenterPanel({ slot, active, publishedView, sessionReady = true }: {
  slot: string | null
  active: boolean
  /** A Crew publication remains readable while its thread is revalidated;
   * native task state and actions wait for that exact session to be confirmed. */
  sessionReady?: boolean
  /** The Crew host supplies its existing published view, with its own sandbox.
   * Presentation composition never grants a document native action authority. */
  publishedView?: { title: string; content: ReactNode }
}) {
  const { t } = useTranslation()
  const data = useCommandCenter(slot, active && sessionReady)
  // Same predicate as TileList's Progress rows: a board stands in for the runs
  // only when it is the progress source (present and nothing omitted). It
  // decides only whether the running runs are listed; work items always are.
  const boardStands = data.progress?.source === 'work'
  const [selected, setSelected] = useState<string | null>(null)
  const views = [
    ...(publishedView ? [{ id: 'crew', title: publishedView.title }] : []),
    ...(sessionReady ? data.dashboards.map(a => ({ id: `artifact:${a.slug}`, title: a.name })) : []),
  ]
  const selectedView = views.find(view => view.id === selected)?.id ?? views[0]?.id
  const [section, setSection] = useState<'dashboard' | 'attention' | 'approvals'>('dashboard')
  const showingOverview = section === 'dashboard' || !sessionReady
  const requestDashboard = useMutation({
    retry: false,
    mutationFn: async () => {
      if (!slot) return
      const receipt = await sendTurn({ slot, message: REQUEST_PUBLISHED_VIEW, steer: 'auto' })
      if (receipt.status !== 'dispatched' && receipt.status !== 'queued') {
        throw new Error(receipt.status === 'refused' ? receipt.reason || t('commandCenter.send_refused') : t('commandCenter.send_unknown'))
      }
    },
  })
  const about = [
    t('commandCenter.description'),
    sessionReady && data.approvalMode === 'normal' ? t('commandCenter.normal_help') : '',
    // Only once there is an agent-designed page to be contained: the same
    // `views` that decide whether a published frame renders below.
    views.length > 0 ? t('commandCenter.contained') : '',
    sessionReady && data.updatedAt > 0 ? t('commandCenter.updated', { time: fmtDateTime(data.updatedAt) }) : '',
  ].filter(Boolean).join(' ')
  return <div className="h-full flex flex-col min-w-0 bg-bg text-text" data-testid="command-center-panel">
    <header className="shrink-0 p-3 border-b border-border space-y-3">
      <div className="flex gap-2 items-center flex-wrap"><LayoutDashboard size={17} className="text-accent" /><h2 tabIndex={-1} {...{ [PANEL_HEADING_ATTR]: '' }} className="font-semibold text-sm outline-hidden">{t('commandCenter.title')}</h2>
        {/* Every explanatory sentence lives behind this one control, so the
            panel itself shows only numbers, requests and the published view. */}
        <InfoTip text={about} />
        {sessionReady && <span className="ml-auto text-[11px] text-muted inline-flex items-center gap-1"><ShieldCheck size={12} />{t('commandCenter.permission_mode', { mode: t(APPROVAL_MODE_KEYS[data.approvalMode]) })}</span>}
      </div>
      <div hidden={!sessionReady} className="space-y-3">
      <StatusTiles data={data} />
      {data.progress && <progress className="w-full h-1.5 accent-accent" value={data.progress.done} max={data.progress.total} aria-label={t('commandCenter.progress_label')} />}
      <SegmentedControl value={section} onChange={setSection} collapse={false} wrap layoutId={`task-dashboard-section-${slot}`} segments={[
        { key: 'dashboard', label: t('commandCenter.dashboard'), icon: <LayoutDashboard size={14} /> },
        { key: 'attention', label: t('commandCenter.needs_input'), icon: <MessageSquare size={14} />, count: data.attention.length - data.approvalCount },
        { key: 'approvals', label: t('commandCenter.approvals'), icon: <ShieldCheck size={14} />, count: data.approvalCount },
      ]} />
      {/* No hand-off: pending QuestionCard answer drafts remain mounted below. */}
      {data.stale && <ErrorNotice message={t('commandCenter.stale')} />}
      {/* No hand-off: the attention cards here can hold unsent QuestionCard answer drafts. */}
      <ErrorNotice message={missingSourcesNotice(data.missing)} />
      </div>
    </header>
    <div className="flex-1 min-h-0 overflow-y-auto">
    <div className="p-3 space-y-3" hidden={!sessionReady || (showingOverview && !data.attention.length)}>
      <PanelSectionHeader label={t('commandCenter.attention_filter')} />
      {!data.stale && !data.attention.some(a => showingOverview || (section === 'approvals' ? a.kind === 'approval' : a.kind !== 'approval')) && <p className="text-sm text-muted p-3">{t('commandCenter.no_input')}</p>}
      {data.attention.map(item => {
        const node = data.nodes.find(n => n.id === `session:${item.slot}`)!
        return <div key={`${slot}:${item.id}`} hidden={!showingOverview && (section === 'approvals' ? item.kind !== 'approval' : item.kind === 'approval')}>
          <AttentionCard item={item} title={runTitle(node)} context={node.detail} onDraftChange={item.question ? active => data.onQuestionDraftChange(item.question!, active) : undefined} />
        </div>
      })}
    </div>
    <div className="p-3 space-y-4" hidden={!showingOverview}>
      {sessionReady && <AutomaticCardSetting active={active && showingOverview} />}
      {views.length > 1 && <label className="flex flex-col gap-1 text-[12px] text-muted">{t('commandCenter.published_view')}
        <SimpleSelect aria-label={t('commandCenter.published_view')} options={views.map(view => view.id)} optionLabels={views.map(view => view.title)} value={selectedView || ''} onChange={setSelected} />
      </label>}
      {publishedView && <div hidden={selectedView !== 'crew'}>{publishedView.content}</div>}
      {data.dashboards.map(artifact => <div key={artifact.slug} hidden={selectedView !== `artifact:${artifact.slug}`}>
        <TaskDashboardFrame artifact={artifact} active={active && sessionReady && showingOverview && selectedView === `artifact:${artifact.slug}`} />
      </div>)}
      {sessionReady && views.length === 0 && <div className="rounded-lg border border-border bg-card p-4 space-y-2">
          <LayoutDashboard size={24} className="text-accent" />
          <h3 className="text-sm font-semibold">{t('commandCenter.adaptive_title')}</h3>
          <p className="text-sm text-muted leading-relaxed">{t('commandCenter.adaptive_description')}</p>
          <Btn disabled={!slot || requestDashboard.isPending || requestDashboard.isSuccess} onClick={() => requestDashboard.mutate()}>{t('commandCenter.request_design')}</Btn>
          {requestDashboard.isSuccess && <p role="status" className="text-sm text-muted">{t('commandCenter.design_requested')}</p>}
          {/* No hand-off: pending QuestionCard answer drafts remain mounted below. */}
          <ErrorNotice message={requestDashboard.error?.message} />
        </div>}
      <div hidden={!sessionReady} className="space-y-3">
      {/* The task's own automatic card. Workers carry none. Under Progress the
          panel shows the work items whenever the board has any, partial or
          not; the running runs are added only when the board is not the
          progress source (absent, or with omitted entries), because that is
          when the dock's Progress list shows runs, and that list caps its rows
          and its overflow lands here, so the rest must be readable somewhere.
          Idle and done runs stay with the sidebar's Subagents and Workflows
          tabs; this is not a roster. */}
      {slot && <SessionStatusFrame slot={slot} title={t('commandCenter.title')} active={active && sessionReady && showingOverview} />}
      {data.blocked > 0 && <>
        <PanelSectionHeader label={t('commandCenter.blocked')} count={data.blocked} />
        <TileList tile="blocked" data={data} />
      </>}
      {(data.loading || data.workItems.length > 0 || (!boardStands && data.running > 0)) && <PanelSectionHeader label={t('commandCenter.tile_progress')} />}
      {data.loading && <p role="status" className="text-sm text-muted">{t('commandCenter.loading')}</p>}
      {data.workItems.map(item => <div key={item.item_id} className="text-[13px] border-l-2 border-border pl-3">
        <p>{item.title}</p><p className="text-muted text-[12px]">{t(STATE_KEYS[item.state])}{item.summary ? ` · ${item.summary}` : ''}</p>
      </div>)}
      {/* No `onOpen`: every running row renders, uncapped. */}
      {!boardStands && data.running > 0 && <TileList tile="progress" data={data} />}
      </div>
    </div>
    </div>
  </div>
}
