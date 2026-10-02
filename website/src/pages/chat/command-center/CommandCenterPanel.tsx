import { useEffect, useRef, useState, type ReactNode } from 'react'
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
import AttentionCard from './AttentionCard'
import { APPROVAL_MODE_KEYS, runTitle } from './model'
import { PANEL_HEADING_ATTR } from './panelHeading'
import { sendTurn } from '../../../chat-core/transport/sendTurn'
import { REQUEST_PUBLISHED_VIEW } from './commandCenter.prompt'

/** The side panel's Dashboard view.
 *
 * The Overview is the agent's published page and nothing else: the host draws
 * the header (title, help, permission mode), the three segments with their
 * counts, and the notices, then hands the whole Overview to the view the agent
 * authored. The blocked and running work, the work items and the progress are
 * the agent's to lay out — `REQUEST_PUBLISHED_VIEW` tells it how — because two
 * renderings of the same numbers, one native and one authored, disagree
 * whenever either lags, and a native block would take the top of a panel meant
 * for the page. The dock above the composer keeps the native tiles; this panel
 * is the page.
 *
 * What stays native is what the agent's HTML must never do: the Questions and
 * Approvals tabs mount the host's own `AttentionCard`s, so an answer or an
 * approval is only ever sent by a control the host rendered. The published page
 * runs sandboxed and can at most point at a decision; it cannot make one.
 *
 * With no published view yet, the Overview shows the automatic card (the
 * gateway's own summary of the session) and the request that asks the agent for
 * a page. Once a page exists the automatic card steps aside: the Overview is the
 * page alone. */
export default function CommandCenterPanel({ slot, active, publishedView, sessionReady = true, onDraftStateChange, onOpenSession }: {
  slot: string | null
  active: boolean
  /** A Crew publication remains readable while its thread is revalidated;
   * native task state and actions wait for that exact session to be confirmed. */
  sessionReady?: boolean
  /** The Crew host supplies its existing published view, with its own sandbox.
   * Presentation composition never grants a document native action authority. */
  publishedView?: { title: string; content: ReactNode }
  /** Called when this panel starts or stops holding a half-entered answer.
   *  A host that can UNMOUNT this subtree needs it: the draft lives only in
   *  `QuestionCard`'s state and this panel's own `drafts`, so an unmount is the
   *  typed text being thrown away, and the host cannot see that from outside.
   *  The Crewmates page keeps its side panel mounted while this is true. */
  onDraftStateChange?: (hasDraft: boolean) => void
  /** How to leave for a session named in this panel, when the HOST must be asked
   *  first. The two Open session affordances -- an attention card's header and a
   *  live-activity row -- are plain `<Link>`s otherwise, and a bare link reaches
   *  no leave guard, so on a host that UNMOUNTS this subtree on a route change
   *  they took the unsent answer with them. Same division as
   *  `onDraftStateChange`: the panel reports and delegates, the host decides.
   *  With no callback the links stay links, which is right on a host the route
   *  change does not unmount. */
  onOpenSession?: (slot: string) => void
}) {
  const { t } = useTranslation()
  const data = useCommandCenter(slot, active && sessionReady)
  // Report the draft state up, and report FALSE on unmount: a host holding its
  // panel open for a draft must not be held by a panel that is no longer there.
  const draftCb = useRef(onDraftStateChange)
  draftCb.current = onDraftStateChange
  const hasDraft = data.hasQuestionDraft
  useEffect(() => { draftCb.current?.(hasDraft) }, [hasDraft])
  useEffect(() => () => { draftCb.current?.(false) }, [])
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
    {/* Questions and Approvals: the host's own cards, every one of them mounted
        whichever tab shows, so a half-typed answer survives a tab switch. Never
        in the Overview — the segments carry their counts, and the published page
        can name a decision but only these cards can make it. */}
    <div className="p-3 space-y-3" hidden={!sessionReady || showingOverview} data-testid="command-center-attention">
      <PanelSectionHeader label={t('commandCenter.attention_filter')} />
      {!data.stale && !data.attention.some(a => section === 'approvals' ? a.kind === 'approval' : a.kind !== 'approval') && <p className="text-sm text-muted p-3">{t('commandCenter.no_input')}</p>}
      {data.attention.map(item => {
        const node = data.nodes.find(n => n.id === `session:${item.slot}`)!
        return <div key={`${slot}:${item.id}`} hidden={section === 'approvals' ? item.kind !== 'approval' : item.kind === 'approval'}>
          <AttentionCard item={item} title={runTitle(node)} context={node.detail} onOpenSession={onOpenSession} onDraftChange={item.question ? active => data.onQuestionDraftChange(item.question!, active) : undefined} />
        </div>
      })}
    </div>
    <div className="p-3 space-y-4" hidden={!showingOverview} data-testid="command-center-overview">
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
      {/* The automatic card — the gateway's own summary of this session — only
          until the agent publishes a page. Once one exists the Overview is that
          page alone, so the two never say different things about one task. */}
      {slot && sessionReady && views.length === 0 && <SessionStatusFrame slot={slot} title={t('commandCenter.title')} active={active && sessionReady && showingOverview} />}
    </div>
    </div>
  </div>
}
