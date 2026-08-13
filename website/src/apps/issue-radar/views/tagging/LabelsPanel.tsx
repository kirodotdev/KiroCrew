import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Plus, RefreshCw, Wand2 } from 'lucide-react'
import {
  issueRadarApi, type LabelRecommendation, type RepoLabel, type RepoSettings, type RepoRef,
} from '../../api'
import { issueUrlFor, repoScopeKey } from '../../lib/links'
import { asArray, readableText, hexToRgba } from '../../lib/format'
import ReadOnlyTag from '../../components/ReadOnlyTag'
import ShimmerLine from '../../components/ShimmerLine'

import { i18nT } from '../../../../i18n/t'
/** The repo's tag vocabulary, in one panel: what it already uses (and how much),
 * and what it is missing.
 *
 * Sorted by usage rather than alphabetically, because the question this answers
 * is "what does this repo actually label with" — the long tail of one-off labels
 * belongs at the bottom. Clicking a tag jumps to the issue list filtered by it.
 *
 * The "Suggest new labels" half proposes labels the repo does NOT have. Creating
 * one writes to GitHub, so it is gated on write access; everything up to that
 * point is a proposal.
 *
 * `onCreated` lets the parent fold a freshly-created label into the repo's local
 * triage roles — a `triage` / `first-issue` proposal is only useful once Issue
 * Radar knows that is what it means.
 */
export default function LabelsPanel({
  repoRef, labels, labelsKnown, countByLabel, canWrite, titleOf, onPick, onCreated,
}: {
  repoRef: RepoRef
  labels: RepoLabel[]
  /** False while the repo's label set is loading or its query failed. An empty
   * `labels` alone cannot tell "this repo has no labels" from "we don't know",
   * and asserting the former is a claim the user may act on. */
  labelsKnown: boolean
  /** Open-issue count per label name. */
  countByLabel: Map<string, number>
  canWrite: boolean
  /** Resolve an example issue number to its title, so a proposal can be judged
   * on the issues it would apply to rather than on bare numbers. */
  titleOf?: (number: number) => string | undefined
  /** Jump to the issue list filtered by this label. */
  onPick?: (name: string) => void
  onCreated?: (rec: LabelRecommendation) => void
}) {
  const qc = useQueryClient()
  const { owner, repo } = repoRef
  const scopeKey = repoScopeKey(repoRef)
  const key = ['issue-radar', 'recommendations', scopeKey]

  const ranked = useMemo(
    () => labels
      .map((l) => ({ ...l, count: countByLabel.get(l.name) ?? 0 }))
      .sort((a, b) => (b.count - a.count) || a.name.localeCompare(b.name)),
    [labels, countByLabel],
  )

  const recoQuery = useQuery({
    queryKey: key,
    queryFn: () => issueRadarApi.getRecommendations(repoRef),
  })
  const recommendations = recoQuery.data?.recommendations ?? null
  const generate = useMutation({
    mutationFn: () => issueRadarApi.generateRecommendations(repoRef),
    onSuccess: (res) => qc.setQueryData(key, res),
  })

  const [created, setCreated] = useState<Set<string>>(new Set())
  const createLabel = useMutation({
    mutationFn: (rec: LabelRecommendation) =>
      issueRadarApi.createLabel(repoRef, {
        name: rec.name, color: rec.color, description: rec.description,
      }),
    onSuccess: (_res, rec) => {
      setCreated((prev) => new Set(prev).add(rec.name))
      // The label now exists on GitHub — refresh every picker that reads the
      // repo's label set, including the untagged queue, which can now stage it.
      qc.invalidateQueries({ queryKey: ['issue-radar', 'labels', scopeKey] })
      onCreated?.(rec)
    },
  })

  /** Every proposal stays on the list: a suggestion you don't want simply isn't
   * created, and the "Suggest again" button replaces the whole set — so a
   * per-row dismiss only added a control that changed nothing durable. */
  const visible = asArray<LabelRecommendation>(recommendations)

  /** Re-fetch the repo's label set from GitHub, bypassing the local cache.
   * Labels are created and renamed on GitHub itself, so without this the panel
   * (and every picker that reads the same query) keeps showing whatever was
   * cached at connect time. Writes into the SHARED labels query the context owns,
   * so one click updates the pickers and the untagged queue too. */
  const refreshLabels = useMutation({
    mutationFn: () => issueRadarApi.labels(repoRef, { refresh: true }),
    onSuccess: (res) => qc.setQueryData(['issue-radar', 'labels', scopeKey], res),
  })

  return (
    <section className="rounded-xl border border-border bg-bg-elevated shadow-sm p-4 flex flex-col gap-3">
      <div className="flex items-center gap-3 flex-wrap">
        <div className="text-[13px] font-semibold text-muted uppercase tracking-[.05em]">
          {i18nT('apps.issueRadar.views.tagging.labelsPanel.labels')}
        </div>
        <div className="text-[12px] text-muted opacity-60">
          {labels.length} {i18nT('apps.issueRadar.views.tagging.labelsPanel.defined_by_open_issue_count')}
        </div>

        <div className="ml-auto flex items-center gap-2.5 flex-wrap">
          {!canWrite && (
            <span className="text-[12px] text-muted inline-flex items-center gap-1">
              <ReadOnlyTag /> {i18nT('apps.issueRadar.views.tagging.labelsPanel.creating_needs_write_access')}
            </span>
          )}
          <button
            onClick={() => refreshLabels.mutate()}
            disabled={refreshLabels.isPending}
            aria-label={i18nT('apps.issueRadar.views.tagging.labelsPanel.re_fetch_this_repo_s_labels_from_github')}
            title={i18nT('apps.issueRadar.views.tagging.labelsPanel.re_fetch_this_repo_s_labels_from_github')}
            className="inline-flex items-center justify-center h-7 w-7 rounded-md border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-40 cursor-pointer"
          >
            <RefreshCw size={12} className={refreshLabels.isPending ? 'animate-spin' : ''} />
          </button>
          <button
            onClick={() => generate.mutate()}
            disabled={generate.isPending}
            title={i18nT('apps.issueRadar.views.tagging.labelsPanel.propose_labels_this_repo_is_missing_from_its_ope')}
            className="inline-flex items-center gap-1.5 text-[13px] px-2.5 py-1 rounded-md border border-accent/40 text-accent hover:bg-accent-subtle disabled:opacity-50 cursor-pointer bg-transparent"
          >
            <Wand2 size={12} className={generate.isPending ? 'animate-pulse' : ''} />
            {generate.isPending
              ? i18nT('apps.issueRadar.views.tagging.labelsPanel.analyzing')
              : recommendations === null ? i18nT('apps.issueRadar.views.tagging.labelsPanel.suggest_new_labels') : i18nT('apps.issueRadar.views.tagging.labelsPanel.suggest_again')}
          </button>
        </div>
      </div>

      {/* What the repo already labels with. */}
      {labels.length === 0 ? (
        <div className="text-[14px] text-muted">
          {labelsKnown
            ? `${owner}/${repo} defines no labels yet — suggest some to get started.`
            : i18nT('apps.issueRadar.views.tagging.labelsPanel.couldn_t_read_this_repo_s_labels_retry_with_the')}
        </div>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {ranked.map((l) => {
            const unused = l.count === 0
            return (
              <button
                key={l.name}
                onClick={() => onPick?.(l.name)}
                title={l.description
                  ? `${l.description} — ${l.count} open`
                  : `${l.count} open issue${l.count === 1 ? '' : 's'}`}
                style={{
                  backgroundColor: hexToRgba(l.color, unused ? 0.08 : 0.2),
                  color: 'var(--text)',
                }}
                className={`inline-flex items-center gap-1.5 max-w-[240px] rounded-full pl-2.5 pr-2 py-0.5 text-[13px] cursor-pointer transition-opacity ${
                  unused ? 'opacity-50' : ''
                }`}
              >
                <span className="truncate">{l.name}</span>
                <span className="tabular-nums text-[12px] opacity-70 flex-shrink-0">{l.count}</span>
              </button>
            )
          })}
        </div>
      )}

      {/* What it is missing. */}
      {(generate.isError || recoQuery.isError || refreshLabels.isError) && (
        <div className="text-[13px] text-danger">
          {((generate.error ?? recoQuery.error ?? refreshLabels.error) as Error)?.message}
        </div>
      )}

      {generate.isPending && (
        <div className="pt-2 border-t border-border flex flex-col gap-2">
          <div className="text-[12px] text-muted">{i18nT('apps.issueRadar.views.tagging.labelsPanel.looking_for_labels_this_repo_is_missing')}</div>
          <SuggestionSkeleton />
        </div>
      )}

      {!generate.isPending && recommendations !== null && (
        <div className="pt-2 border-t border-border flex flex-col gap-0.5">
          <div className="text-[12px] text-muted mb-0.5">
            {i18nT('apps.issueRadar.views.tagging.labelsPanel.suggested_new_labels_not_created_until_you_say_s')}
          </div>

          {visible.length === 0 ? (
            <div className="text-[13px] text-muted">
              {i18nT('apps.issueRadar.views.tagging.labelsPanel.none_the_repo_s_taxonomy_already_covers_what_its')}
            </div>
          ) : visible.map((rec) => {
            const isCreated = created.has(rec.name)
            const isCreating = createLabel.isPending && createLabel.variables?.name === rec.name
            const failed = createLabel.isError && createLabel.variables?.name === rec.name
            const examples = asArray<number>(rec.examples)
            // One short line of "why", not a paragraph: the rationale is the
            // grounded half, the description is the fallback.
            const why = rec.rationale || rec.description
            return (
              // Hover highlights the WHOLE row, not just the button: the row is
              // a line of small controls, and without a band to track it is easy
              // to lose which proposal the Create button under the cursor belongs
              // to. `group` also brings the example link up with it.
              //
              // Two columns, not two stacked lines: the text (label + reason,
              // then its example) grows downward on the left while the actions
              // stay vertically CENTRED against the whole row — top-aligning
              // them left the buttons floating above the row they act on.
              // `border border-transparent` is not decoration: the queue rows
              // carry a 1px border (it turns accent when selected), so without a
              // matching one here the two panels' content edges sit 1px apart.
              <div key={rec.name} className="group rounded-md border border-transparent px-2 py-2 -mx-2 hover:bg-bg-hover transition-colors">
                <div className="flex items-center gap-2 min-w-0">
                  <div className="flex-1 min-w-0 flex flex-col gap-1">
                    <div className="flex items-center gap-2 min-w-0">
                      <span
                        className="flex-shrink-0 inline-flex items-center rounded-full px-2 py-0.5 text-[12px] font-semibold max-w-[200px]"
                        style={{ backgroundColor: `#${rec.color}`, color: readableText(rec.color) }}
                      >
                        <span className="truncate">{rec.name}</span>
                      </span>
                      <span className="flex-1 min-w-0 truncate text-[13px] text-muted" title={why}>
                        {why}
                      </span>
                    </div>

                    {/* The evidence: ONE real issue from THIS repo that would get
                      * the label, as `#number: title`, so the proposal can be
                      * judged without opening anything. One is enough to make the
                      * case — a list of three turned every row into a paragraph.
                      * The reason line above stays prose-only. */}
                    {examples.length > 0 && (
                      <div className="flex flex-col gap-0 ml-1">
                        {examples.slice(0, 1).map((n) => {
                          const t = titleOf?.(n)
                          // No truncation and no `title`: an issue title is the
                          // whole point of the line, so it wraps rather than being
                          // cut, and a tooltip would only repeat what is already
                          // on screen (while shadowing the accessible name).
                          return (
                            <a
                              key={n}
                              href={issueUrlFor(repoRef, n)}
                              target="_blank"
                              rel="noreferrer"
                              className="text-[12px] text-muted opacity-60 group-hover:opacity-90 hover:!opacity-100 hover:text-accent hover:underline transition-opacity"
                            >
                              <span className="tabular-nums font-medium">#{n}</span>
                              {t ? `: ${t}` : ''}
                            </a>
                          )
                        })}
                      </div>
                    )}
                  </div>

                  <div className="flex-shrink-0 flex items-center gap-2">
                    {isCreated ? (
                      <span className="inline-flex items-center gap-1 text-[12px] text-accent">
                        <Check size={12} /> {i18nT('apps.issueRadar.views.tagging.labelsPanel.created')}
                      </span>
                    ) : (
                      <button
                        onClick={() => createLabel.mutate(rec)}
                        // Disabled while ANY create is in flight, not just this
                        // row's: each one patches the same local label cache, and
                        // two overlapping patches lose one of the new labels.
                        disabled={!canWrite || createLabel.isPending}
                        aria-label={i18nT('apps.issueRadar.views.tagging.labelsPanel.create_the_label_on_github', { name: rec.name })}
                        title={canWrite ? i18nT('apps.issueRadar.views.tagging.labelsPanel.create_this_label_on_github') : i18nT('apps.issueRadar.views.tagging.labelsPanel.read_only_repo_needs_triage_push_access')}
                        // Same accent treatment as the queue's Add button: both are
                        // the row's one write action, so they should read alike.
                        className="inline-flex items-center gap-1 text-[12px] px-2 py-0.5 rounded border border-accent/40 text-accent hover:bg-accent-subtle disabled:opacity-30 disabled:hover:bg-transparent cursor-pointer bg-transparent"
                      >
                        <Plus size={11} className={isCreating ? 'animate-pulse' : ''} />
                        {isCreating ? i18nT('apps.issueRadar.views.tagging.labelsPanel.creating') : i18nT('apps.issueRadar.views.tagging.labelsPanel.create')}
                      </button>
                    )}
                  </div>
                </div>
                {failed && (
                  <div className="text-[12px] text-danger ml-1 mb-1">
                    {(createLabel.error as Error).message}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

/** The shape a suggestion will take, animated while one is being produced.
 * Deliberately mirrors the real row (label pill + one-line reason + example
 * issues) so the panel doesn't reflow when the results land — and says nothing
 * about how the work happens. */
function SuggestionSkeleton() {
  return (
    <div className="flex flex-col gap-2.5" aria-hidden="true">
      {[0, 1, 2].map((i) => (
        <div key={i} className="flex flex-col gap-1">
          <div className="flex items-center gap-2">
            <ShimmerLine w="88px" delay={i * 0.18} />
            <ShimmerLine w={`${180 + i * 46}px`} delay={i * 0.18 + 0.09} />
          </div>
          <div className="ml-1">
            <ShimmerLine w={`${140 + i * 30}px`} delay={i * 0.18 + 0.18} />
          </div>
        </div>
      ))}
    </div>
  )
}

/** Which local triage role (if any) a freshly-created label should join, so the
 * caller can mirror the settings page's behaviour without duplicating the map. */
export function settingsKeyForCategory(
  category: LabelRecommendation['category'],
): keyof Pick<RepoSettings, 'triage_labels' | 'good_first_issue_labels'> | null {
  if (category === 'triage') return 'triage_labels'
  if (category === 'first-issue') return 'good_first_issue_labels'
  return null
}
