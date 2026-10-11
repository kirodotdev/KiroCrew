import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery } from '@tanstack/react-query'
import { LayoutDashboard, MessageSquare, ShieldCheck } from 'lucide-react'
import { PanelSectionHeader } from '../../../components/ui'
import SegmentedControl from '../../../components/SegmentedControl'
import ErrorNotice from '../../../components/ErrorNotice'
import InfoTip from '../../../components/InfoTip'
import { fmtDateTime } from '../../../i18n/format'
import { useAppSelector } from '../../../store'
import { membersRosterQuery } from '../../../api/membersQuery'
import { missingSourcesNotice, useCommandCenter } from './useCommandCenter'
import AttentionCard, { RestoredQuestionNotice } from './AttentionCard'
import { APPROVAL_MODE_KEYS, runTitle } from './model'
import { PANEL_HEADING_ATTR } from './panelHeading'

const CrewDynamicDashboard = lazy(() => import('../../members/CrewDynamicDashboard'))

/** The side panel's Dashboard view.
 *
 * The Overview is the ROOT session's own Dynamic Dashboard: the same frame a
 * crewmate's Dashboard tab draws, keyed by this session's slot, so every field
 * is read from this session's own work ledger and crew log and the page comes
 * from the shared template catalog. A dispatched or adopted session has no
 * dashboard of its own -- its records are on its dispatcher's board -- so the
 * Overview segment is dropped and the panel opens on Questions. The gateway
 * applies the same root test (`is_root_session`) and refuses the read; this
 * check only keeps the panel from offering a page it would be refused.
 *
 * The Questions and Approvals tabs mount the host's own `AttentionCard`s, so an
 * answer or an approval is only ever sent by a control the host rendered. The
 * dashboard runs sandboxed and can at most point at a decision; it cannot make
 * one. */
export default function CommandCenterPanel({ slot, active, sessionReady = true, onDraftStateChange, onOpenSession, onAct }: {
  slot: string | null
  active: boolean
  /** Native task state and actions wait for this exact session to be confirmed. */
  sessionReady?: boolean
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
  /** Put a reply the dashboard page offered into this session's composer. */
  onAct?: (text: string) => void
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
  // A ROOT session: one no other session dispatched (`created_by`) or adopted
  // (`parent`). The gateway decides with the same two readings and refuses the read
  // otherwise, so this only withholds a segment it would refuse.
  const owner = useAppSelector(s => slot ? s.dashboard.slots.find(item => item.key === slot) : undefined)
  const root = !!slot && !!owner && !owner.created_by && !owner.parent
  // A crewmate's DM slot is a root too, but its agent writes the CREWMATE's page,
  // not a session page, so the Overview shows that page. Decided by the roster's
  // own slot binding; until the roster answers, nothing is mounted rather than a
  // session page that may be the wrong one. A failed roster read is said, not
  // guessed around: without the roster this panel cannot tell a crewmate's DM from
  // a plain session, so it shows neither page.
  const roster = useQuery({ ...membersRosterQuery, enabled: root && active })
  const dmRows = (roster.data ?? []).filter(r => !!slot && r.slot_key === slot)
  const dm = dmRows.length === 1 ? dmRows[0] : null
  const rosterFailed = !dm && roster.isError
  const target = dm
    ? { kind: 'member' as const, slug: dm.slug, member: dm.name }
    : roster.isPending || rosterFailed ? null : { kind: 'session' as const, slot: slot ?? '' }
  const [chosen, setSection] = useState<'dashboard' | 'attention' | 'approvals'>('dashboard')
  const section = !root && chosen === 'dashboard' ? 'attention' : chosen
  const showingOverview = root && (section === 'dashboard' || !sessionReady)
  const about = [
    t('commandCenter.description'),
    sessionReady && data.approvalMode === 'normal' ? t('commandCenter.normal_help') : '',
    sessionReady && data.updatedAt > 0 ? t('commandCenter.updated', { time: fmtDateTime(data.updatedAt) }) : '',
  ].filter(Boolean).join(' ')
  return <div className="h-full flex flex-col min-w-0 bg-bg text-text" data-testid="command-center-panel">
    <header className="shrink-0 p-3 border-b border-border space-y-3">
      <div className="flex gap-2 items-center flex-wrap"><LayoutDashboard size={17} className="text-accent" /><h2 tabIndex={-1} {...{ [PANEL_HEADING_ATTR]: '' }} className="font-semibold text-sm outline-hidden">{t('commandCenter.title')}</h2>
        {/* Every explanatory sentence lives behind this one control, so the
            panel itself shows only counts and the dashboard. */}
        <InfoTip text={about} />
        {sessionReady && <span className="ml-auto text-[11px] text-muted inline-flex items-center gap-1"><ShieldCheck size={12} />{t('commandCenter.permission_mode', { mode: t(APPROVAL_MODE_KEYS[data.approvalMode]) })}</span>}
      </div>
      <div hidden={!sessionReady} className="space-y-3">
      <SegmentedControl value={section} onChange={setSection} collapse={false} wrap layoutId={`task-dashboard-section-${slot}`} segments={[
        ...(root ? [{ key: 'dashboard' as const, label: t('commandCenter.dashboard'), icon: <LayoutDashboard size={14} /> }] : []),
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
        in the Overview — the segments carry their counts, and the dashboard can
        name a decision but only these cards can make it. */}
    {/* A dispatched or adopted session has no Overview of its own; say where its
        work shows instead of letting the segment vanish without a reason. */}
    {!root && slot && <p className="px-3 pt-3 text-[12px] text-muted" data-testid="command-center-no-dashboard">{t('commandCenter.no_dashboard_here')}</p>}
    <div className="p-3 space-y-3" hidden={(root && !sessionReady) || showingOverview} data-testid="command-center-attention">
      <PanelSectionHeader label={t('commandCenter.attention_filter')} />
      {!data.stale
        && !data.attention.some(a => section === 'approvals' ? a.kind === 'approval' : a.kind !== 'approval')
        && (section === 'approvals' || !data.restoredQuestionNotices.length)
        && <p className="text-sm text-muted p-3">{t('commandCenter.no_input')}</p>}
      {data.attention.map(item => {
        const node = data.nodes.find(n => n.id === `session:${item.slot}`)!
        return <div key={`${slot}:${item.id}`} hidden={section === 'approvals' ? item.kind !== 'approval' : item.kind === 'approval'}>
          <AttentionCard item={item} title={runTitle(node)} context={node.detail} onOpenSession={onOpenSession} onDraftChange={item.question ? answers => data.onQuestionDraftChange(item.question!, answers) : undefined} />
        </div>
      })}
      {data.restoredQuestionNotices.map(notice => {
        const node = data.nodes.find(n => n.id === `session:${notice.slot}`)
        return <div key={notice.id} hidden={section === 'approvals'}>
          <RestoredQuestionNotice
            slot={notice.slot}
            question={notice.question}
            title={node ? runTitle(node) : undefined}
            onDismiss={() => data.dismissRestoredQuestionNotice(notice.id)}
          />
        </div>
      })}
    </div>
    {root && slot && <div className="h-full min-h-[24rem] flex flex-col" hidden={!showingOverview} data-testid="command-center-overview">
      {/* Mounted only while shown: a hidden frame would keep re-reading a page
          nobody is looking at. */}
      {/* No hand-off: the Questions tab beside this can hold unsent answer drafts. */}
      {active && showingOverview && rosterFailed && <ErrorNotice className="m-3" message={t('pages.membersPage.dashboard_unavailable')} testId="command-center-roster-error" />}
      {active && showingOverview && target && <Suspense fallback={null}>
        <CrewDynamicDashboard
          key={target.kind === 'member' ? `member:${target.slug}` : slot}
          target={target}
          displayName={dm ? dm.display_name || dm.name : owner?.title || t('commandCenter.title')}
          onAct={onAct}
        />
      </Suspense>}
    </div>}
    </div>
  </div>
}
