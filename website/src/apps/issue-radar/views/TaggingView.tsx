import { useCallback, useEffect, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Check, RefreshCw, Sparkles } from 'lucide-react'
import {
  issueRadarApi, type BulkApplyResponse, type GenerateTaggingResponse,
  type LabelRecommendation, type SuggestedLabel, type TaggingResponse,
  type UntaggedIssue,
} from '../api'
import { useIssueRadar } from '../context'
import { asArray } from '../lib/format'
import ReadOnlyTag from '../components/ReadOnlyTag'
import UntaggedIssueCard from './tagging/UntaggedIssueCard'
import LabelsPanel, { settingsKeyForCategory } from './tagging/LabelsPanel'
import { repoScopeKey } from '../lib/links'

import { i18nT } from '../../../i18n/t'
/** Tagging dashboard — bulk-label triage for the issues that have no labels at all.
 *
 * Three stacked blocks, top to bottom: the numbers, the repo's tag vocabulary,
 * and the untagged queue with its actions. There is no page title — the KPI strip
 * IS the header, since the left rail already says which dashboard you're on.
 *
 * The loop is suggest → review → confirm, and every step is reversible until the
 * last one:
 *
 *   1. The queue lists every open issue with ZERO labels (the honest definition
 *      of "untagged" — an issue with a `bug` label is tagged even if it still
 *      needs triage).
 *   2. One batched model call proposes labels for a slice of that queue, chosen
 *      ONLY from labels the repo already defines. Nothing is written yet.
 *   3. You can unstage any proposal, hand-pick extra labels, then apply one issue
 *      or every staged issue at once.
 *
 * Staging is client-side and user-owned: `overrides` shadows the server's
 * proposal per issue, so a refresh or a regenerate never silently undoes an edit.
 */
/** Remount the dashboard whenever the active repo changes.
 *
 * Every piece of review state below is keyed by ISSUE NUMBER — staged overrides,
 * the selection, the applied set — and issue numbers collide across repos. A
 * long-lived mount would therefore carry `bug` staged for #12 in repo A over to
 * #12 in repo B and let Apply write it there. Keying is the whole fix; there is
 * no per-field reset to forget. */
export default function TaggingView() {
  const { active } = useIssueRadar()
  return <TaggingDashboard key={`${active.owner}/${active.repo}`} />
}

function TaggingDashboard() {
  const {
    active, repoLabels, canWrite, labelsLoading, labelsError, toggleLabel, openIssues,
  } = useIssueRadar()
  const { owner, repo } = active
  const scopeKey = repoScopeKey(active)
  const qc = useQueryClient()
  const taggingKey = useMemo(() => ['issue-radar', 'tagging', scopeKey], [scopeKey])

  const taggingQuery = useQuery({
    queryKey: taggingKey,
    queryFn: () => issueRadarApi.tagging(active),
  })

  // Memoized: `stagedFor` and the derived counts depend on this map, and a fresh
  // `{}` literal on every render would re-run all of them.
  const suggestions = useMemo<Record<string, SuggestedLabel[]>>(
    () => taggingQuery.data?.suggestions ?? {},
    [taggingQuery.data],
  )
  const batchSize = taggingQuery.data?.batch_size ?? 50

  /** The untagged issues, exactly as the server ordered them (newest first).
   * Carried in the response rather than resolved against the shared issue list:
   * that list follows the user's open/closed filter, so entering Tagging from a
   * Closed filter produced an empty queue. */
  const queue = useMemo(
    () => asArray<UntaggedIssue>(taggingQuery.data?.issues),
    [taggingQuery.data],
  )
  /** Label counts and example titles, both served by /tagging rather than derived
   * from the shared issue list: that list follows the user's open/closed filter,
   * so entering Tagging from a Closed filter reported closed counts as open ones
   * and lost the example titles entirely. */
  const labelCounts = useMemo(
    () => new Map(Object.entries(taggingQuery.data?.label_counts ?? {})),
    [taggingQuery.data],
  )
  const titleOf = useCallback(
    (n: number): string | undefined => taggingQuery.data?.titles?.[String(n)] || undefined,
    [taggingQuery.data],
  )

  // ── staging ──
  // `overrides` is only populated for issues the user actually edited, so an
  // untouched row keeps tracking the server's latest proposal.
  const [overrides, setOverrides] = useState<Record<number, string[]>>({})

  /** Issues whose labels have reached GitHub, mapped to what was written.
   * The row STAYS in the list — removing it made the list jump under the cursor
   * and left no confirmation of what had just happened — so this is what freezes
   * it: chips go static, the action collapses to "Added", and it drops out of
   * `applicable` so a later Apply cannot re-write it. A reload naturally clears
   * these rows, because the server stops reporting them as untagged. */
  const [applied, setApplied] = useState<Record<number, string[]>>({})

  /** Label names the repo currently defines. A proposal naming anything else is
   * unusable: the backend rejects an unknown label and fails the WHOLE bulk
   * chunk, so one label renamed or deleted on GitHub would block every issue in
   * the batch. Filtering here means a refresh silently drops the obsolete
   * proposals instead. `applied` rows are exempt — they record what was actually
   * written, which stays true even if the label is deleted afterwards. */
  const knownLabels = useMemo(() => new Set(repoLabels.map((l) => l.name)), [repoLabels])
  /** True only when the repo's label set is genuinely known. `knownLabels.size`
   * alone cannot tell "this repo has no labels" from "the labels query failed",
   * and treating the second as the first bypassed the filter entirely — leaving
   * stale proposals applicable and the queue claiming the repo has no labels. */
  const labelsKnown = !labelsLoading && !labelsError

  const stagedFor = useCallback(
    (n: number): string[] => applied[n]
      ?? (overrides[n] ?? (suggestions[String(n)] ?? []).map((s) => s.name))
        // When the set is known, `has` is the only test: an empty known set means
        // every repo label was deleted, so nothing is applicable — keeping the
        // proposals staged left Add enabled for a request the backend rejects.
        .filter((name) => !labelsKnown || knownLabels.has(name)),
    [applied, overrides, suggestions, knownLabels, labelsKnown],
  )
  const stage = (n: number, names: string[]) =>
    setOverrides((prev) => ({ ...prev, [n]: names }))

  /** The queue MINUS rows already written. Everything about re-running and the
   * Untagged count is derived from this, not from `queue`: an applied row is no
   * longer untagged, and a re-run slice made of only applied issues came back
   * with `analyzed: []`, so the cursor never advanced and the rows past it were
   * unreachable. */
  const pending = useMemo(() => queue.filter((i) => !(i.number in applied)), [queue, applied])


  /** Where the next re-run starts, once every queued issue has been analysed
   * once. Without it, "Suggest again" re-analysed `queue.slice(0, batchSize)`
   * every time, so on a queue longer than one batch the issues past the first
   * slice were unreachable. Wraps at the end. */
  const [rerunCursor, setRerunCursor] = useState(0)

  const [selected, setSelected] = useState<Set<number>>(new Set())
  const toggleSelect = (n: number) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(n)) next.delete(n)
      else next.add(n)
      return next
    })

  /** Issues that would actually be written by "apply all": everything staged,
   * narrowed to the selection when one exists. Selecting nothing means "all",
   * which is what the button label says. */
  const applicable = useMemo(
    () => queue
      .map((i) => ({ number: i.number, add: stagedFor(i.number) }))
      .filter((c) => c.add.length > 0
        && !(c.number in applied)
        && (selected.size === 0 || selected.has(c.number))),
    [queue, stagedFor, selected, applied],
  )
  const analyzedCount = useMemo(
    () => pending.filter((i) => String(i.number) in suggestions).length,
    [pending, suggestions],
  )

  /** Reload the queue from GITHUB, not from the local cache.
   * A plain refetch re-served the same cached issue list, so an issue labelled on
   * GitHub itself never left the queue however many times you pressed reload. */
  const reload = useMutation({
    mutationFn: () => issueRadarApi.tagging(active, { refresh: true }),
    onSuccess: (res) => {
      qc.setQueryData<TaggingResponse>(taggingKey, res)
      // The refreshed issue set is the app's too. Queue-keyed state is reconciled
      // by the effect below, which covers this refetch and every other one.
      qc.invalidateQueries({ queryKey: ['issue-radar', 'issues', scopeKey] })
    },
  })

  /** Drop queue-keyed state for issues the queue no longer contains — on EVERY
   * change to the query data, not just the manual reload.
   *
   * react-query also refetches on window focus and reconnect, and those paths
   * never touched this state: an issue labelled on GitHub disappeared from the
   * queue while its selection survived, which kept "selection mode" on while
   * matching no current row, so Apply looked armed and silently covered nothing. */
  // TWO separate dependencies, because the two jobs key off different sets.
  //
  // Pruning must follow the FULL queue: an applied issue that leaves the queue and
  // is later re-added would otherwise keep its stale `applied` entry and stay
  // frozen as "Added" forever, because it never appears in `pending`.
  const fullQueueKey = queue.map((i) => i.number).join(',')
  useEffect(() => {
    const live = new Set(queue.map((i) => i.number))
    const prune = <T,>(m: Record<number, T>): Record<number, T> => Object.fromEntries(
      Object.entries(m).filter(([n]) => live.has(Number(n))),
    ) as Record<number, T>
    const keep = (m: Record<number, unknown>) =>
      Object.keys(m).every((n) => live.has(Number(n)))

    setSelected((prev) => {
      const next = new Set([...prev].filter((n) => live.has(n)))
      return next.size === prev.size ? prev : next
    })
    setOverrides((prev) => (keep(prev) ? prev : prune(prev)))
    setApplied((prev) => (keep(prev) ? prev : prune(prev)))
    // Keyed on membership rather than the array identity: re-running on every
    // render would fight the setState calls above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fullQueueKey])

  // The cursor follows PENDING: applying a row shrinks it without changing the
  // queue, and an un-clamped cursor then points past the end, producing an empty
  // re-run slice that can never advance.
  const pendingKey = pending.map((i) => i.number).join(',')
  useEffect(() => {
    setRerunCursor((c) => (c >= pending.length ? 0 : c))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingKey])

  // ── generate (batched) ──
  const [genError, setGenError] = useState<string | null>(null)
  const applyGenerated = (res: GenerateTaggingResponse) => {
    // Patch the cached response rather than invalidating: the server returns the
    // merged suggestion map, and a refetch would also re-list the queue mid-review.
    qc.setQueryData<TaggingResponse>(taggingKey, (prev) =>
      prev ? { ...prev, suggestions: res.suggestions, generated_at: res.generated_at } : prev)
    // A regenerated issue gets the fresh proposal — drop the stale override so
    // the new suggestion is what shows.
    setOverrides((prev) => {
      const next = { ...prev }
      for (const n of res.analyzed) delete next[n]
      return next
    })
    // Only now advance the re-run cursor: this slice is genuinely done.
    if (res.analyzed.length > 0) {
      setRerunCursor((c) => (c + res.analyzed.length >= pending.length ? 0 : c + res.analyzed.length))
    }
  }
  const generate = useMutation({
    mutationFn: (numbers?: number[]) => issueRadarApi.generateTagging(active, numbers),
    onMutate: () => setGenError(null),
    onSuccess: applyGenerated,
    onError: (e: Error) => setGenError(e.message),
  })
  const suggestingFor = generate.isPending ? generate.variables : undefined
  const generatingBatch = generate.isPending && !suggestingFor

  // ── apply ──
  const [rowErrors, setRowErrors] = useState<Record<number, string>>({})
  const [applyNote, setApplyNote] = useState<string | null>(null)
  const [applyError, setApplyError] = useState<string | null>(null)

  const markApplied = (rows: { number: number; labels: string[] }[]) => {
    setApplied((prev) => {
      const next = { ...prev }
      for (const r of rows) next[r.number] = r.labels
      return next
    })
    // Patch the served label counts too. The queue rows deliberately stay put
    // (see the refetchType below), so nothing else refreshes them — the Labels
    // panel would keep showing usage counts from before these writes.
    qc.setQueryData<TaggingResponse>(taggingKey, (prev) => {
      if (!prev) return prev
      const counts = { ...prev.label_counts }
      for (const r of rows) {
        for (const name of r.labels) counts[name] = (counts[name] ?? 0) + 1
      }
      return { ...prev, label_counts: counts }
    })
    const done = new Set(rows.map((r) => r.number))
    setSelected((prev) => new Set([...prev].filter((n) => !done.has(n))))
    // The labelled issues are no longer untagged anywhere else in the app.
    qc.invalidateQueries({ queryKey: ['issue-radar', 'issues', scopeKey] })
    // Mark the queue response stale too, but do NOT refetch: the rows must stay
    // put so the list does not jump. Without this the response stayed fresh for
    // its whole staleTime, so a remount reset `applied` while restoring the stale
    // queue — re-enabling rows that had already been written.
    qc.invalidateQueries({ queryKey: taggingKey, refetchType: 'none' })
  }

  const applyOne = useMutation({
    mutationFn: ({ number, add }: { number: number; add: string[] }) =>
      issueRadarApi.applyLabels(active, number, add, []),
    onMutate: ({ number }) => {
      setRowErrors((p) => {
        const { [number]: _dropped, ...rest } = p
        return rest
      })
      // The bulk banner ("… N could not be updated") described a previous batch.
      // Leaving it up after a retry succeeds reports a failure that no longer
      // exists, so it is cleared when the retry starts.
      setApplyNote(null)
      setApplyError(null)
    },
    onSuccess: (res, { number, add }) => markApplied([
      // Prefer the authoritative set the API returns; fall back to what we sent.
      { number, labels: res.labels?.length ? res.labels.map((l) => l.name) : add },
    ]),
    onError: (e: Error, { number }) => setRowErrors((p) => ({ ...p, [number]: e.message })),
  })

  /** The backend caps ONE bulk request (it fans out to that many sequential `gh`
   * calls), so a queue larger than the cap is sent as successive requests rather
   * than one rejected 400. Sequential, not parallel: these are writes against the
   * same repo, and the cap exists to bound how long GitHub is being hammered.
   *
   * Read from the response, not hardcoded — a client-side copy silently turns
   * every large bulk apply into a 400 the day the backend cap changes. */
  const BULK_CHUNK = taggingQuery.data?.bulk_max ?? 25

  const applyAll = useMutation({
    mutationFn: async (changes: { number: number; add: string[] }[]) => {
      const merged = {
        owner, repo,
        applied: [] as BulkApplyResponse['applied'],
        failed: [] as BulkApplyResponse['failed'],
      }
      for (let i = 0; i < changes.length; i += BULK_CHUNK) {
        const res = await issueRadarApi.applyLabelsBulk(active, changes.slice(i, i + BULK_CHUNK))
        merged.applied.push(...res.applied)
        merged.failed.push(...res.failed)
        // Reconcile as we go. If a LATER chunk rejects, the mutation's onSuccess
        // never runs — so without this the rows already written to GitHub would
        // stay unmarked and be offered for writing again.
        markApplied(res.applied.map((r) => ({
          number: r.number,
          labels: r.labels?.length
            ? r.labels.map((l) => l.name)
            : (changes.find((c) => c.number === r.number)?.add ?? []),
        })))
        // Same reasoning for the failures this chunk reported: onSuccess never
        // runs if a LATER chunk rejects, so publishing there would lose them.
        if (res.failed.length > 0) {
          setRowErrors((prev) => ({
            ...prev,
            ...Object.fromEntries(res.failed.map((f) => [f.number, f.error])),
          }))
        }
      }
      return merged
    },
    onMutate: (changes) => {
      setApplyError(null)
      setApplyNote(null)
      // Clear only the rows THIS apply covers. Wiping the map erased unresolved
      // errors for issues outside the current selection, so a failure the user
      // has not addressed silently disappeared.
      const covered = new Set(changes.map((c) => c.number))
      setRowErrors((prev) => Object.fromEntries(
        Object.entries(prev).filter(([n]) => !covered.has(Number(n))),
      ))
    },
    onSuccess: (res) => {
      // Rows are already marked per chunk (see mutationFn) — this only reports.
      // Partial failure is expected (a locked or transferred issue). Report it
      // per row AND in the banner instead of pretending the batch succeeded.
      if (res.failed.length > 0) {
        setApplyNote(
          `Labelled ${res.applied.length} issue${res.applied.length === 1 ? '' : 's'}; `
          + `${res.failed.length} could not be updated (see the rows below).`,
        )
      } else {
        setApplyNote(`Labelled ${res.applied.length} issue${res.applied.length === 1 ? '' : 's'}.`)
      }
    },
    onError: (e: Error) => setApplyError(e.message),
  })

  /** Mirror the settings page: a newly-created `triage` / `first-issue` label
   * joins the corresponding local role, so the dashboards understand it.
   *
   * The append happens SERVER-SIDE (`addSettingLabel`), under the config lock.
   * The settings PUT replaces the whole document, so any client-side
   * read-modify-write — even a perfectly chained one — only serializes itself:
   * two dashboard tabs each read the same settings, both issue a full
   * replacement, and the later write permanently drops the other's label.
   * Chaining cannot fix that; moving the read and the write into one critical
   * section on the server can. Failures surface in the banner instead of hiding
   * behind a green "Created". */
  const [settingsError, setSettingsError] = useState<string | null>(null)

  const onLabelCreated = (rec: LabelRecommendation) => {
    const key = settingsKeyForCategory(rec.category)
    if (!key) return
    issueRadarApi.addSettingLabel(active, key, rec.name)
      .then((res) => qc.setQueryData(['issue-radar', 'settings', scopeKey], res))
      .catch((e: Error) => setSettingsError(
        i18nT('apps.issueRadar.views.taggingView.created_but_settings_not_updated', { name: rec.name, error: e.message }),
      ))
  }

  /** True while ANY apply is in flight. Every apply mutates the same server-side
   * issue cache, and those writes are not serialized across requests, so a second
   * apply started mid-flight can clobber the first one's cache patch. One at a
   * time: a pending apply disables every other apply control, not just its own row. */
  const applyBusy = applyOne.isPending || applyAll.isPending
    // Applying needs the repo's label set: a name that has been renamed away is
    // rejected for the WHOLE bulk chunk, so a failed labels query must block the
    // write rather than send a guess.
    || !labelsKnown

  const noLabels = labelsKnown && repoLabels.length === 0
  const allSelected = queue.length > 0 && selected.size === queue.length
  /** Untagged issues that have never been analysed. Drives the button: once this
   * hits zero the "next slice" is empty and the backend would no-op, so the
   * button switches to an explicit re-run over the queue instead of offering a
   * pass that does nothing. */
  const unanalyzed = Math.max(pending.length - analyzedCount, 0)
  const exhausted = pending.length > 0 && unanalyzed === 0
  const nextSlice = Math.min(batchSize, exhausted ? pending.length - rerunCursor : unanalyzed)
  /** The issues the next re-run covers: `batchSize` of them starting at the
   * cursor, so pressing the button repeatedly advances instead of looping over
   * the first slice forever. */
  const rerunSlice = () => pending.slice(rerunCursor, rerunCursor + batchSize)
  /** True while a WHOLE-QUEUE pass is running (a fresh slice or a re-run), as
   * opposed to the single-issue Suggest on one row. */
  const batchPending = generate.isPending && (suggestingFor === undefined || suggestingFor.length > 1)

  return (
    <div className="px-6 pt-4 pb-6 flex flex-col gap-4">
      {/* KPI strip — this is the page header. Two numbers only: the size of the
        * queue, and the vocabulary available to clear it. "Analysed" and "Ready
        * to apply" are already legible from the rows and the Apply button. */}
      <div className="grid grid-cols-2 gap-3">
        <Stat value={pending.length} label={i18nT('apps.issueRadar.views.taggingView.untagged')} sub={
          taggingQuery.data?.open_count
            ? `${Math.round((pending.length / taggingQuery.data.open_count) * 100)}% of open`
            : ''
        } />
        <Stat value={repoLabels.length} label={i18nT('apps.issueRadar.views.taggingView.repo_labels')} sub={i18nT('apps.issueRadar.views.taggingView.available_to_assign')} />
      </div>

      {/* The repo's tag vocabulary — what it uses, and what it's missing. */}
      <LabelsPanel
        repoRef={active}
        labels={repoLabels}
        labelsKnown={labelsKnown}
        countByLabel={labelCounts}
        canWrite={canWrite}
        titleOf={titleOf}
        onPick={(name) => { toggleLabel(name); openIssues() }}
        onCreated={onLabelCreated}
      />

      {/* The queue and its actions are one feature — same panel. */}
      <section className="rounded-xl border border-border bg-bg-elevated shadow-sm p-4 flex flex-col gap-3">
        <div className="flex items-center gap-2.5 flex-wrap">
          <div className="text-[13px] font-semibold text-muted uppercase tracking-[.05em]">
            {i18nT('apps.issueRadar.views.taggingView.untagged_issues')}
          </div>
          <div className="text-[12px] text-muted opacity-60">{i18nT('apps.issueRadar.views.taggingView.newest_first')}</div>

          <div className="ml-auto flex items-center gap-2 flex-wrap">
            {queue.length > 0 && (
              <button
                onClick={() => setSelected(allSelected ? new Set() : new Set(queue.map((i) => i.number)))}
                className="text-[12px] text-muted hover:text-text cursor-pointer bg-transparent px-1"
              >
                {allSelected ? i18nT('apps.issueRadar.views.taggingView.clear_selection') : i18nT('apps.issueRadar.views.taggingView.select_all')}
              </button>
            )}
            {!canWrite && (
              <span className="text-[12px] text-muted inline-flex items-center gap-1">
                <ReadOnlyTag /> {i18nT('apps.issueRadar.views.taggingView.applying_needs_write_access')}
              </span>
            )}
            <button
              onClick={() => reload.mutate()}
              disabled={reload.isPending}
              aria-label={i18nT('apps.issueRadar.views.taggingView.reload_the_untagged_queue')}
              title={i18nT('apps.issueRadar.views.taggingView.reload_the_untagged_queue')}
              className="inline-flex items-center justify-center h-7 w-7 rounded-md border border-border text-muted hover:text-text hover:border-border-strong disabled:opacity-40 cursor-pointer"
            >
              <RefreshCw size={12} className={reload.isPending ? 'animate-spin' : ''} />
            </button>
            <button
              onClick={() => {
                if (!exhausted) { generate.mutate(undefined); return }
                // Every queued issue already has a proposal, so ask for those
                // numbers explicitly — omitting them would select the (empty)
                // un-analysed slice and the request would do nothing. Start from
                // the cursor so successive re-runs walk the whole queue. The
                // cursor advances in the mutation's onSuccess, not here: moving it
                // up-front made a failed request skip that slice entirely until
                // the cursor wrapped around.
                generate.mutate(rerunSlice().map((i) => i.number))
              }}
              disabled={generate.isPending || pending.length === 0 || noLabels}
              title={exhausted
                ? `Re-analyse ${nextSlice} issue(s) that already have a proposal`
                : `Analyse the next ${nextSlice} issue(s)`}
              className="inline-flex items-center gap-1.5 text-[13px] px-2.5 py-1 rounded-md border border-accent/40 text-accent hover:bg-accent-subtle disabled:opacity-40 cursor-pointer bg-transparent"
            >
              <Sparkles size={12} className={batchPending ? 'animate-pulse' : ''} />
              {batchPending
                ? i18nT('apps.issueRadar.views.taggingView.analyzing')
                : exhausted
                  ? i18nT('apps.issueRadar.views.taggingView.suggest_again', { n: nextSlice })
                  : i18nT('apps.issueRadar.views.taggingView.suggest_labels_next', { n: nextSlice })}
            </button>
            <button
              onClick={() => applyAll.mutate(applicable)}
              disabled={!canWrite || applyBusy || applicable.length === 0}
              title={canWrite
                ? i18nT('apps.issueRadar.views.taggingView.write_every_staged_label_to_github')
                : i18nT('apps.issueRadar.views.taggingView.read_only_repo_needs_triage_or_push_access')}
              className="inline-flex items-center gap-1.5 text-[13px] px-2.5 py-1 rounded-md bg-accent text-white hover:opacity-90 disabled:opacity-40 cursor-pointer"
            >
              <Check size={12} />
              {applyAll.isPending
                ? i18nT('apps.issueRadar.views.taggingView.applying')
                : `Apply ${applicable.length} suggestion${applicable.length === 1 ? '' : 's'}`}
            </button>
          </div>
        </div>

        {noLabels && (
          <Banner kind="warn">
            {owner}/{repo} {i18nT('apps.issueRadar.views.taggingView.defines_no_labels_yet_so_there_is_nothing_to_ass')}
          </Banner>
        )}
        {labelsError && (
          <Banner kind="error">
            {i18nT('apps.issueRadar.views.taggingView.couldn_t_load')} {owner}/{repo}{i18nT('apps.issueRadar.views.taggingView.s_labels')} {(labelsError as Error).message}
            {' — '}{i18nT('apps.issueRadar.views.taggingView.applying_is_disabled_until_they_load_because_a_s')}
          </Banner>
        )}
        {settingsError && <Banner kind="error">{settingsError}</Banner>}
        {reload.isError && (
          <Banner kind="error">
            {i18nT('apps.issueRadar.views.taggingView.couldn_t_reload_the_queue')} {(reload.error as Error).message}
            {' — '}{i18nT('apps.issueRadar.views.taggingView.what_you_see_below_is_the_previous_result_not_th')}
          </Banner>
        )}
        {genError && <Banner kind="error">{genError}</Banner>}
        {applyError && <Banner kind="error">{applyError}</Banner>}
        {applyNote && <Banner kind={Object.keys(rowErrors).length ? 'warn' : 'ok'}>{applyNote}</Banner>}

        {taggingQuery.isLoading ? (
          <div className="text-[14px] text-muted py-2">{i18nT('apps.issueRadar.views.taggingView.loading_the_untagged_queue')}</div>
        ) : taggingQuery.isError ? (
          <Banner kind="error">{(taggingQuery.error as Error).message}</Banner>
        ) : queue.length === 0 ? (
          <div className="text-[14px] text-muted py-2">
            {i18nT('apps.issueRadar.views.taggingView.every_open_issue_in')} {owner}/{repo} {i18nT('apps.issueRadar.views.taggingView.carries_at_least_one_label_nothing_to_tag')}
          </div>
        ) : (
          <div className="flex flex-col gap-0.5">
            {queue.map((iss) => (
              <UntaggedIssueCard
                key={iss.number}
                issue={iss}
                labels={repoLabels}
                staged={stagedFor(iss.number)}
                suggestions={suggestions[String(iss.number)] ?? []}
                analyzed={String(iss.number) in suggestions}
                canWrite={canWrite}
                selected={selected.has(iss.number)}
                onToggleSelect={() => toggleSelect(iss.number)}
                onStage={(names) => stage(iss.number, names)}
                onApply={() => applyOne.mutate({ number: iss.number, add: stagedFor(iss.number) })}
                applying={
                  (applyOne.isPending && applyOne.variables?.number === iss.number)
                  || (applyAll.isPending && applicable.some((c) => c.number === iss.number))
                }
                busy={applyBusy}
                suggesting={
                  !!suggestingFor?.includes(iss.number)
                  || (generatingBatch && !(String(iss.number) in suggestions))
                }
                applied={iss.number in applied}
                error={rowErrors[iss.number]}
              />
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

// ── local presentational helpers (kept in-file so the view stays one unit) ──

function Stat({ value, label, sub }: { value: number; label: string; sub?: string }) {
  return (
    <div className="rounded-xl border border-border bg-bg-elevated shadow-sm p-4">
      <div className="text-[30px] font-bold text-text leading-none tabular-nums">{value}</div>
      <div className="text-[13px] text-muted mt-1.5">{label}</div>
      {sub && <div className="text-[12px] text-muted opacity-60 mt-0.5">{sub}</div>}
    </div>
  )
}

function Banner({ kind, children }: { kind: 'ok' | 'warn' | 'error'; children: React.ReactNode }) {
  const cls = kind === 'error'
    ? 'border-danger/40 bg-danger/5 text-danger'
    : kind === 'warn'
      ? 'border-border bg-bg-hover text-text'
      : 'border-accent/40 bg-accent-subtle text-accent'
  return (
    <div className={`rounded-lg border px-3 py-2 text-[13px] flex items-start gap-2 ${cls}`}>
      {kind === 'ok'
        ? <Check size={13} className="flex-shrink-0 mt-0.5" />
        : <AlertTriangle size={13} className="flex-shrink-0 mt-0.5" />}
      <span className="min-w-0">{children}</span>
    </div>
  )
}
