// Learning: the ruleset a review actually loads, for ONE namespace at a time.
//
// Namespace selection and management live in the rail (LearningRail) — this is the
// detail pane, the same rail-picks / pane-reads split the review surface uses. It
// was previously a single page of nested accordions, which meant the patterns you
// came to read were two clicks deep and the repo picker stayed on screen above
// them.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Brain, Loader2, Wand2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { sageApi } from '../api'
import { useSage } from '../context'
import { LIVE_POLL_MS } from '../lib/layout'
import type { LearnedPattern } from '../lib/types'

import { i18nT } from '../../../i18n/t'
function ImpactTag({ impact }: { impact: string }) {
  const high = impact === 'high'
  return (
    <span className={`inline-block px-1.5 py-0.5 rounded text-[10px] mr-1.5 ${
      high ? 'text-danger border border-danger' : 'text-muted border border-border'
    }`}>
      {impact}
    </span>
  )
}

function PatternRow({ p }: { p: LearnedPattern }) {
  return (
    <li className="rounded-lg border border-border bg-card px-3.5 py-2.5">
      <div className="text-[13px]">
        <ImpactTag impact={p.impact} />
        <strong className="text-text">{p.title}</strong>
      </div>
      <div className="mt-1 text-[12.5px] text-muted leading-[1.6]">{p.guidance}</div>
    </li>
  )
}

/** Run the one-shot merge that turns staged candidates into the live ruleset.
 *
 * Two clicks: consolidating REPLACES the ruleset and clears the candidate, and
 * neither the old file nor the staged list is recoverable from here. The merge
 * itself is a worker turn, so it takes a while and reports its own outcome —
 * a failed merge leaves the ruleset untouched and says so. */
// Exported for test: the disarm-on-namespace-change guard has to be observable without
// the parent's mount/unmount behaviour standing in for it. While the new namespace's
// learnings load the parent briefly renders no candidates, which unmounts this control and
// resets its state incidentally -- so a test driven through the parent passes even with the
// guard removed.
export function ConsolidateControl({
  namespace, count, running, error,
}: {
  namespace: string
  count: number
  running: boolean
  error: string | null
}) {
  const qc = useQueryClient()
  const [confirming, setConfirming] = useState(false)
  // An armed consolidation belongs to the namespace that was selected when it was armed.
  // Carrying it across a namespace change means the second click replaces a DIFFERENT
  // namespace's ruleset and clears its staged candidates -- the one irreversible write in
  // the learning path, aimed at something the reader never chose.
  useEffect(() => { setConfirming(false) }, [namespace])
  const mut = useMutation({
    mutationFn: () => sageApi.consolidateLearnings(namespace),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['code-review-sage', 'learnings', namespace] })
      void qc.invalidateQueries({ queryKey: ['code-review-sage', 'namespaces'] })
    },
  })
  const busy = running || mut.isPending

  return (
    <span className="ml-auto inline-flex items-center gap-2">
      {error && !busy && (
        <span className="inline-flex flex-col gap-0.5 max-w-[38ch] text-[11px] text-danger text-right">
          <span>{i18nT('apps.codeReviewSage.views.learningView.merge_failed_ruleset_unchanged')}</span>
          {/* Visible, not tooltip-only — the same reason a refused post shows its
              cause: a hover target tells a keyboard or touch user nothing. */}
          <span className="break-words opacity-90">{error}</span>
        </span>
      )}
      {busy ? (
        <span className="inline-flex items-center gap-1.5 text-[11.5px] text-muted">
          <Loader2 size={11} className="animate-spin motion-reduce:animate-none" />
          {i18nT('apps.codeReviewSage.views.learningView.consolidating')}
        </span>
      ) : confirming ? (
        <>
          <span className="text-[11.5px] text-muted">
            {/* One question, one key: the count sits inside it so a
                translator can place it and inflect the noun. */}
            {i18nT('apps.codeReviewSage.views.learningView.confirm_merge', { count })}
          </span>
          <button
            type="button"
            onClick={() => { setConfirming(false); mut.mutate() }}
            className="rounded bg-transparent px-1 text-[11.5px] font-medium text-accent hover:underline cursor-pointer"
          >
            {i18nT('apps.codeReviewSage.views.learningView.consolidate')}
          </button>
          <button
            type="button"
            onClick={() => setConfirming(false)}
            className="rounded bg-transparent px-1 text-[11.5px] text-muted hover:text-text cursor-pointer"
          >
            {i18nT('apps.codeReviewSage.views.learningView.cancel')}
          </button>
        </>
      ) : (
        <button
          type="button"
          onClick={() => setConfirming(true)}
          aria-label={i18nT('apps.codeReviewSage.views.learningView.consolidate_pending',
            { count, namespace })}
          className="inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-0.5 text-[11.5px] text-text hover:text-accent hover:border-accent cursor-pointer"
        >
          <Wand2 size={11} aria-hidden="true" />
          {i18nT('apps.codeReviewSage.views.learningView.consolidate')}
        </button>
      )}
      {mut.error && (
        <span className="text-[11px] text-danger">{(mut.error as Error).message}</span>
      )}
    </span>
  )
}

export default function LearningView() {
  const { selectedNamespace } = useSage()
  const ns = selectedNamespace

  const learningsQuery = useQuery({
    queryKey: ['code-review-sage', 'learnings', ns],
    queryFn: () => sageApi.learnings(ns as string),
    enabled: !!ns,
    // A merge is a worker turn, so its result arrives out of band. Poll only
    // while one is running; otherwise this file changes when a human edits it.
    refetchInterval: (q) => (q.state.data?.consolidating ? LIVE_POLL_MS : false),
  })
  const settingsQuery = useQuery({
    queryKey: ['code-review-sage', 'settings'],
    queryFn: () => sageApi.settings(),
  })

  const activeList = settingsQuery.data?.settings.active_namespaces ?? []
  const isActive = !!ns && activeList.includes(ns)
  const patterns = learningsQuery.data?.patterns ?? []
  const candidate = learningsQuery.data?.candidate ?? []

  if (!ns) {
    return (
      <div className="h-full overflow-y-auto scrollbar-none px-6 py-6">
        <h1 className="text-[22px] font-bold leading-tight text-text-strong flex items-center gap-2">
          <Brain size={18} className="text-accent" aria-hidden="true" /> {i18nT('apps.codeReviewSage.views.learningView.learning')}
        </h1>
        <p className="text-[13px] text-muted mt-1.5 leading-[1.5] max-w-[620px]">
          {i18nT('apps.codeReviewSage.views.learningView.pick_a_namespace_in_the_sidebar_to_read_the_patt')}
        </p>
      </div>
    )
  }

  return (
    <div className="h-full overflow-y-auto scrollbar-none px-6 py-6">
      <div className="max-w-[820px]">
        <h1 className="text-[22px] font-bold leading-tight text-text-strong flex items-center gap-2">
          <Brain size={18} className="text-accent" aria-hidden="true" />
          <span className="font-mono">{ns}</span>
          {/* Whether reviews actually load this namespace is the first thing worth
              knowing about it — a ruleset you are reading may be switched off. */}
          <span className={`text-[11px] px-2 py-0.5 rounded-full border ${
            isActive
              ? 'border-accent text-accent bg-accent-subtle'
              : 'border-border text-muted'
          }`}>
            {isActive
              ? i18nT('apps.codeReviewSage.views.learningView.loaded_during_reviews')
              : i18nT('apps.codeReviewSage.views.learningView.not_loaded')}
          </span>
        </h1>
        <p className="text-[13px] text-muted mt-1.5 leading-[1.5] max-w-[620px]">
          {i18nT('apps.codeReviewSage.views.learningView.reviews_read_the_consolidated_ruleset_below_neve')}
        </p>

        {learningsQuery.isLoading && (
          <div className="mt-6 inline-flex items-center gap-2 text-[13px] text-muted">
            <Loader2 size={14} className="animate-spin motion-reduce:animate-none" />
            {i18nT('apps.codeReviewSage.views.learningView.loading_learnings')}
          </div>
        )}
        {learningsQuery.error && (
          <div className="mt-6 text-[13px] text-danger">
            {(learningsQuery.error as Error).message}
          </div>
        )}

        {!learningsQuery.isLoading && (
          <>
            <div className="mt-6 flex items-center gap-2">
              <h2 className="text-[11px] font-semibold uppercase tracking-wider text-muted">
                {i18nT('apps.codeReviewSage.views.learningView.ruleset')} {i18nT('apps.codeReviewSage.views.learningView.pattern', { count: patterns.length })}
              </h2>
            </div>
            {patterns.length === 0 ? (
              <div className="mt-2 text-[12.5px] text-muted italic leading-[1.5]">
                {i18nT('apps.codeReviewSage.views.learningView.nothing_consolidated_yet_reviews_in_this_namespa')}
              </div>
            ) : (
              <ul className="list-none p-0 mt-2 flex flex-col gap-2">
                {patterns.map((p) => <PatternRow key={p.id} p={p} />)}
              </ul>
            )}

            <div className="mt-7 flex items-center gap-2">
              <h2 className="text-[11px] font-semibold uppercase tracking-wider text-warn">
                {i18nT('apps.codeReviewSage.views.learningView.pending_consolidation')} {candidate.length}
              </h2>
              {candidate.length > 0 && (
                <ConsolidateControl
                  namespace={ns}
                  count={candidate.length}
                  running={Boolean(learningsQuery.data?.consolidating)}
                  error={learningsQuery.data?.consolidate_error ?? null}
                />
              )}
            </div>
            {candidate.length === 0 ? (
              <div className="mt-2 text-[12.5px] text-muted italic leading-[1.5]">
                {i18nT('apps.codeReviewSage.views.learningView.nothing_staged_new_learnings_land_here_when_a_re')}
              </div>
            ) : (
              <ul className="list-none p-0 mt-2 flex flex-col gap-2">
                {candidate.map((c) => (
                  <li key={c.id} className="rounded-lg border border-warn/40 bg-card px-3.5 py-2.5">
                    <div className="text-[13px]">
                      <ImpactTag impact={c.impact} />
                      <strong className="text-text">{c.title}</strong>
                    </div>
                    <div className="mt-1 text-[12.5px] text-muted leading-[1.6]">
                      {c.guidance}
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>
    </div>
  )
}
