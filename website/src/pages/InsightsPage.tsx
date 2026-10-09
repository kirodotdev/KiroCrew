import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Copy, RotateCcw, ShieldAlert } from 'lucide-react'
import { api } from '../api/client'
import type { InsightsAction, InsightsApplyResult, InsightsPriorAction, InsightsView } from '../api/client/personalInsights'
import { Badge, Btn, PageHeader, Skeleton } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { copyToClipboard } from '../utils/clipboard'
import { fmtDateTime } from '../i18n/format'
import { i18nT } from '../i18n/t'

const errMsg = (e: unknown): string => (e instanceof Error && e.message ? e.message : String(e))

const QUERY_KEY = ['personal-insights', 'latest'] as const

function ctaLabel(action: InsightsAction): string {
  switch (action.action_class) {
    case 'lesson_proposal': return i18nT('pages.insightsPage.cta_lesson_proposal')
    case 'prompt': return i18nT('pages.insightsPage.cta_prompt')
    case 'steering_patch': return i18nT('pages.insightsPage.cta_steering_patch')
    default: return i18nT('pages.insightsPage.cta_existing_capability')
  }
}

function artifactLabel(action: InsightsAction): string {
  switch (action.action_class) {
    case 'lesson_proposal': return i18nT('pages.insightsPage.label_lesson')
    case 'prompt': return i18nT('pages.insightsPage.label_prompt')
    case 'steering_patch': return i18nT('pages.insightsPage.label_steering')
    default: return i18nT('pages.insightsPage.label_capability')
  }
}

function stateBadge(state: string) {
  if (state === 'applied_verified') return <Badge variant="ok">{i18nT('pages.insightsPage.state_done_verified')}</Badge>
  if (state === 'changed_unverified') return <Badge variant="warn">{i18nT('pages.insightsPage.state_changed_unverified')}</Badge>
  if (state === 'undone') return <Badge variant="muted">{i18nT('pages.insightsPage.state_undone')}</Badge>
  if (state === 'held') return <Badge variant="warn">{i18nT('pages.insightsPage.state_held')}</Badge>
  return null
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <Btn
      type="button"
      className="text-xs py-1.5 px-3 inline-flex items-center gap-1.5"
      onClick={async () => {
        if (await copyToClipboard(text)) {
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1800)
        }
      }}
    >
      {copied ? <Check size={13} aria-hidden /> : <Copy size={13} aria-hidden />}
      {copied ? i18nT('pages.insightsPage.copied') : i18nT('pages.insightsPage.copy')}
    </Btn>
  )
}

function HeldNotice({ result, onForce, busy }: { result: InsightsApplyResult; onForce: () => void; busy: boolean }) {
  const overfit = result.overfit
  return (
    <div role="status" className="mt-3 rounded-md border border-warn bg-warn-subtle text-text px-3 py-2.5 text-xs leading-relaxed">
      <div className="flex items-center gap-1.5 font-semibold">
        <ShieldAlert size={13} aria-hidden />
        {i18nT('pages.insightsPage.held_title')}
      </div>
      {overfit && (
        <div className="mt-1 text-muted">
          {i18nT('pages.insightsPage.overfit_summary', {
            sessions: overfit.supporting_sessions,
            lineages: overfit.independent_lineages,
            days: overfit.distinct_days,
          })}
        </div>
      )}
      {overfit?.reasons?.length ? (
        <ul className="mt-1 list-disc pl-4 font-mono text-[11px]">
          {overfit.reasons.map(r => <li key={r}>{r}</li>)}
        </ul>
      ) : (
        <div className="mt-1 font-mono text-[11px]">{result.message}</div>
      )}
      <div className="mt-2">
        <Btn type="button" className="text-xs py-1.5 px-3" disabled={busy} onClick={onForce}>
          {i18nT('pages.insightsPage.apply_anyway')}
        </Btn>
      </div>
    </div>
  )
}

function ActionCard({ action }: { action: InsightsAction }) {
  const queryClient = useQueryClient()
  const [held, setHeld] = useState<InsightsApplyResult | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const apply = useMutation({
    mutationFn: (force: boolean) => api.personalInsightsDoIt(action.action_id, force),
    onSuccess: (result) => {
      setFailure(null)
      if (result.state === 'applied_verified' || result.state === 'undone') {
        setHeld(null)
        void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
      } else {
        setHeld(result)
      }
    },
    onError: (e) => setFailure(errMsg(e)),
  })
  const undo = useMutation({
    mutationFn: () => api.personalInsightsUndo(action.action_id),
    onSuccess: (result) => {
      setFailure(null)
      if (result.state === 'undone') {
        setHeld(null)
        void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
      } else {
        setFailure(result.message)
      }
    },
    onError: (e) => setFailure(errMsg(e)),
  })
  const busy = apply.isPending || undo.isPending
  const applied = action.state === 'applied_verified' || action.state === 'changed_unverified'

  return (
    <section
      data-testid="insights-action"
      data-state={action.state}
      className="border border-border bg-card text-card-fg rounded-lg p-4 shadow-sm"
    >
      <div className="flex flex-wrap items-start gap-2">
        <h3 className="text-sm font-semibold flex-1 min-w-0">
          {action.rank != null ? `${action.rank}. ` : ''}{action.title}
        </h3>
        {stateBadge(action.state)}
      </div>
      <p className="mt-2 text-sm leading-relaxed">{action.why}</p>
      <p className="mt-1 text-xs text-muted">
        {i18nT('pages.insightsPage.evidence', { sessions: action.evidence_sessions, total: action.evidence_total })}
      </p>
      <div className="mt-3 text-[11px] text-muted">{artifactLabel(action)}</div>
      <pre className="mt-1 whitespace-pre-wrap break-words rounded-md border border-border bg-bg px-3 py-2 text-xs font-mono leading-relaxed">
        {action.display_artifact}
      </pre>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-muted">{i18nT('pages.insightsPage.expected_later')}</dt>
        <dd>{action.expected_observation}</dd>
        <dt className="text-muted">{i18nT('pages.insightsPage.verify')}</dt>
        <dd>{action.verification}</dd>
        <dt className="text-muted">{i18nT('pages.insightsPage.undo')}</dt>
        <dd>{action.undo}</dd>
      </dl>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {action.executable && !applied && (
          <Btn
            type="button"
            primary
            className="text-xs py-1.5 px-3.5"
            disabled={busy}
            onClick={() => apply.mutate(false)}
          >
            {apply.isPending ? i18nT('pages.insightsPage.applying') : ctaLabel(action)}
          </Btn>
        )}
        {action.executable && applied && (
          <Btn
            type="button"
            className="text-xs py-1.5 px-3.5 inline-flex items-center gap-1.5"
            disabled={busy}
            onClick={() => undo.mutate()}
          >
            <RotateCcw size={13} aria-hidden />
            {undo.isPending ? i18nT('pages.insightsPage.undoing') : i18nT('pages.insightsPage.undo')}
          </Btn>
        )}
        <CopyButton text={action.display_artifact} />
        {!action.executable && (
          <span className="text-xs text-muted">{i18nT('pages.insightsPage.copy_first')}</span>
        )}
        {applied && action.applied_at != null && (
          <span className="text-xs text-muted">
            {i18nT('pages.insightsPage.applied_at', { when: fmtDateTime(action.applied_at * 1000) })}
          </span>
        )}
      </div>
      {held && <HeldNotice result={held} busy={busy} onForce={() => apply.mutate(true)} />}
      {failure && <div role="alert" className="mt-2 text-xs text-danger">{failure}</div>}
    </section>
  )
}

function PriorActions({ items, minSessions }: { items: InsightsPriorAction[]; minSessions: number }) {
  const queryClient = useQueryClient()
  const [failure, setFailure] = useState<string | null>(null)
  const undo = useMutation({
    mutationFn: (actionId: string) => api.personalInsightsUndo(actionId),
    onSuccess: (result) => {
      if (result.state === 'undone') {
        setFailure(null)
        void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
      } else {
        setFailure(result.message)
      }
    },
    onError: (e) => setFailure(errMsg(e)),
  })
  if (items.length === 0) return null
  return (
    <section className="border border-border bg-card text-card-fg rounded-lg p-4 shadow-sm">
      <h2 className="text-sm font-semibold">{i18nT('pages.insightsPage.applied_earlier')}</h2>
      <p className="mt-1 text-xs text-muted">
        {i18nT('pages.insightsPage.follow_through_hint', { min: minSessions })}
      </p>
      <ul className="mt-2 divide-y divide-border">
        {items.map(item => (
          <li key={item.action_id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
            <span className="flex-1 min-w-0">{item.title}</span>
            {stateBadge(item.state)}
            {item.applied_at != null && (
              <span className="text-xs text-muted">
                {i18nT('pages.insightsPage.applied_at', { when: fmtDateTime(item.applied_at * 1000) })}
              </span>
            )}
            <Btn
              type="button"
              className="text-xs py-1 px-3 inline-flex items-center gap-1.5"
              disabled={undo.isPending}
              onClick={() => undo.mutate(item.action_id)}
            >
              <RotateCcw size={13} aria-hidden />
              {i18nT('pages.insightsPage.undo_it')}
            </Btn>
          </li>
        ))}
      </ul>
      {failure && <div role="alert" className="mt-2 text-xs text-danger">{failure}</div>}
    </section>
  )
}

export default function InsightsPage() {
  const query = useQuery<InsightsView>({
    queryKey: QUERY_KEY,
    queryFn: () => api.personalInsightsLatest(),
    retry: false,
  })
  const view = query.data
  const noRuns = query.isError && /no_runs|404/.test(errMsg(query.error))

  return (
    <>
      <PageHeader
        title={i18nT('pages.insightsPage.title')}
        subtitle={i18nT('pages.insightsPage.subtitle')}
      />
      <div className="mx-auto w-full max-w-[880px] px-4 pb-8 flex flex-col gap-4">
        {query.isPending && <Skeleton className="h-40 w-full" />}
        {noRuns && <p className="text-sm text-muted">{i18nT('pages.insightsPage.no_runs')}</p>}
        {query.isError && !noRuns && (
          <ErrorNotice message={`${i18nT('pages.insightsPage.load_failed')}: ${errMsg(query.error)}`} />
        )}
        {view && (
          <>
            <p className="text-xs text-muted" data-testid="insights-run-line">
              {i18nT('pages.insightsPage.run_line', {
                analyzed: view.run.analyzed,
                cataloged: view.run.cataloged,
                when: fmtDateTime(view.run.created_at * 1000),
              })}
            </p>
            <div className="msg-content text-sm leading-relaxed">
              <MarkdownRenderer content={view.report_before} />
            </div>
            <PriorActions items={view.prior_actions} minSessions={view.follow_through_min_sessions} />
            <h2 className="text-base font-semibold mt-2">{i18nT('pages.insightsPage.actions_heading')}</h2>
            {view.actions.length === 0 && (
              <p className="text-sm text-muted">{i18nT('pages.insightsPage.no_actions')}</p>
            )}
            {view.actions.map(action => <ActionCard key={action.action_id} action={action} />)}
            <div className="msg-content text-sm leading-relaxed mt-2">
              <MarkdownRenderer content={view.report_after} />
            </div>
          </>
        )}
      </div>
    </>
  )
}
