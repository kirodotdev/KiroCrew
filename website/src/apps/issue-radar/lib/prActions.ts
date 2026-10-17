// The pull-request ACTION layer: one React Query mutation per action, shared by
// the per-PR bar in the detail pane and the bulk bar in the list.
//
// Why one module rather than a mutation per component: every one of these actions
// invalidates the SAME set of caches (the PR's detail, the list row it lives on,
// and — for close/reopen — which list it belongs to at all). Spreading that
// bookkeeping across call sites is how one action ends up leaving a stale card
// behind while another does not. Here the invalidation is written once, keyed off
// the action, so a new action inherits it.
//
// Merging comes in two forms and NEITHER can bypass a gate: the provider enforces
// branch protection on both of its endpoints, so an unsatisfied PR is refused
// server-side. `merge` is for a PR that is mergeable now; `setAutoMerge` is for one
// that should land by itself once its checks pass. `merge` is per-PR only — it is
// irreversible, so it is absent from the bulk allowlist.
import { useCallback, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  issueRadarApi,
  type BulkPrAction, type BulkPrResponse, type RepoRef,
} from '../api'
import { repoScopeKey } from './links'
import {
  MERGE_READY_STATES as READY_STATES, MERGE_STATE_DIRTY, MERGE_STATE_UNKNOWN,
  MERGE_METHOD,
} from './wireValues'

/**
 * Fallback chunk size when the server has not published its cap.
 *
 * Only reached by a cached response with no `bulk_max` field; deliberately
 * conservative so the fallback can never itself exceed the real cap.
 */
export const DEFAULT_BULK_CHUNK = 25

/**
 * A merge method, in the provider-neutral vocabulary the API speaks.
 *
 * The union is GitHub's, which is the wider of the two: GitLab's `/merge` has no
 * rebase option (merge-vs-rebase is a project setting there, and the only
 * per-request lever is `squash`), so the server refuses `REBASE` for a GitLab repo
 * with a 400 rather than quietly producing a merge commit under a rebase label. No
 * UI path can hit that — nothing here offers a method picker and every call defaults
 * to `SQUASH`, which both providers accept — so this stays one type rather than
 * splitting per provider for a value the UI never sends.
 */
export type MergeMethod = typeof MERGE_METHOD[keyof typeof MERGE_METHOD]

// `MERGE_READY_STATES` and `MERGE_METHOD` are PROTOCOL values; see `lib/wireValues.ts`,
// which is where every by-value provider/server string in this surface lives.
export { MERGE_READY_STATES, MERGE_METHOD } from './wireValues'

/**
 * Whether a row/detail is mergeable RIGHT NOW, from its provider merge state.
 *
 * The single readiness predicate for both bars. Returns false for an unknown state,
 * which is the load-bearing half: GitHub computes mergeability asynchronously and
 * answers `unknown` on a cold read, and a gate that cannot tell must refuse rather
 * than guess in either direction.
 */
export const isMergeReady = (mergeableState?: string | null): boolean =>
  READY_STATES.has((mergeableState ?? '').toLowerCase())

/** The shape both bucket predicates read. */
export interface MergeCandidate {
  state?: string
  draft?: boolean
  merged_at?: string | null
  mergeable_state?: string | null
}

/**
 * Whether a row is still OPEN and therefore a candidate for either merge verb.
 *
 * The ONE lifecycle gate, shared by both buckets rather than open-coded in each. The
 * two predicates below are deciding the same security-relevant question from the same
 * fields, and the warning on `MERGE_READY_STATES` applies just as much here: two copies
 * diverge, and the first review of this change caught exactly that — one copy had a
 * `draft` guard and the other did not.
 *
 * `merged_at` is checked BEFORE `state` on purpose: the backend corrects a stale row's
 * state, but a row can also carry a merge timestamp while its cached `state` still says
 * open, and a merged PR is finished whichever field reveals it.
 */
const isOpenCandidate = (pr: MergeCandidate): boolean =>
  !pr.merged_at && (pr.state ?? '').toLowerCase() !== 'closed' && !pr.draft

/**
 * Whether arming the provider's auto-merge is MEANINGFUL for this row.
 *
 * The complement of {@link isMergeReady}, and deliberately not its negation. The
 * provider refuses to arm in four distinct cases, and every one of them was being
 * discovered one failed row at a time by a bulk bar that had no readiness field:
 *
 * - already mergeable — "Pull request is in clean status";
 * - already merged — "Pull request is already merged";
 * - a DRAFT — a draft cannot be armed (the per-PR bar has always checked this);
 * - `dirty` — a PR with merge CONFLICTS cannot be armed either; nothing will resolve
 *   the conflict on its own, so there is no "once checks pass" to wait for.
 *
 * A row whose readiness is UNKNOWN is in neither bucket: GitHub computes mergeability
 * asynchronously, and on a cold read roughly half a page can report `unknown`. Guessing
 * there produces exactly the failed batch this predicate exists to prevent.
 */
export const canArmAutoMerge = (pr: MergeCandidate): boolean => {
  if (!isOpenCandidate(pr)) return false
  const state = (pr.mergeable_state ?? '').toLowerCase()
  if (!state || state === MERGE_STATE_UNKNOWN) return false
  // `dirty` is a CONFLICT: arming waits for checks, and no check resolves a conflict.
  if (state === MERGE_STATE_DIRTY) return false
  return !READY_STATES.has(state)
}

/**
 * Whether this row can be merged RIGHT NOW — the other half of the partition.
 *
 * Same lifecycle gate as {@link canArmAutoMerge}, then an affirmative readiness check.
 * `draft` is excluded here too: a draft reporting `clean` (transiently, or from a stale
 * cache row) would otherwise be offered a merge the provider only refuses.
 */
export const canMergeNow = (pr: MergeCandidate): boolean =>
  isOpenCandidate(pr) && isMergeReady(pr.mergeable_state)

/** What a completed bulk run produced, for the summary the list bar shows.
 * `failed` is a first-class field, not an error: a batch in which one PR was
 * locked and nine succeeded is a partial success, and reporting it as a thrown
 * error would tell the user nothing applied. */
export interface BulkOutcome {
  action: BulkPrAction
  applied: number[]
  failed: Array<{ number: number; error: string }>
}

/** Actions that can move a PR between the open and closed lists, so the LIST
 * itself has to be refetched rather than just the row patched. A merge closes the
 * PR, so it belongs here too. */
const LIFECYCLE_ACTIONS = new Set<string>(['close', 'reopen', 'merge'])

/**
 * The wire/`busy` identifiers for each action, as one `as const` map.
 *
 * These are PROTOCOL VALUES, not user copy — they are the server's action names and
 * the keys a component compares `busy` against. Collecting them here rather than
 * inlining the literals keeps that distinction legible (a bare `'request_changes'`
 * in JSX reads like a label), and gives the components one place to reference so a
 * renamed action cannot drift between the caller and the `busy` check.
 */
export const PR_ACTION = {
  close: 'close',
  reopen: 'reopen',
  approve: 'approve',
  requestChanges: 'request_changes',
  comment: 'comment',
  merge: 'merge',
  autoMerge: 'auto_merge',
  cancelAutoMerge: 'cancel_auto_merge',
  cancelRun: 'cancel_run',
  rerunRun: 'rerun_run',
} as const

/** The provider-side review verbs, keyed by our action name. */
const REVIEW_EVENT = {
  approve: 'approve',
  requestChanges: 'request_changes',
} as const

/**
 * Invalidate exactly what an action changed.
 *
 * A lifecycle change (close/reopen) moves the PR between lists, so both lists are
 * refetched. Everything else leaves the PR where it is, so only its detail (and
 * the runs behind the CI actions) is dropped — refetching a 50-row list to reflect
 * an approval would be the wrong trade, and the server has already patched its own
 * caches either way.
 */
function useInvalidatePr(ref: RepoRef) {
  const queryClient = useQueryClient()
  const scopeKey = repoScopeKey(ref)
  return useCallback(
    (numbers: number[], action: string) => {
      for (const number of numbers) {
        queryClient.invalidateQueries({ queryKey: ['issue-radar', 'pull', scopeKey, number] })
        queryClient.invalidateQueries({ queryKey: ['issue-radar', 'pull-runs', scopeKey, number] })
      }
      if (LIFECYCLE_ACTIONS.has(action)) {
        // Both the plain list and the person-filtered search, since either may be
        // the rendered source (they are mutually exclusive but which one is live
        // is not this layer's business).
        queryClient.invalidateQueries({ queryKey: ['issue-radar', 'pulls', scopeKey] })
        queryClient.invalidateQueries({ queryKey: ['issue-radar', 'pulls-search', scopeKey] })
      }
    },
    [queryClient, scopeKey],
  )
}

/**
 * The per-PR actions for one pull request.
 *
 * Each returns a promise that RESOLVES on success and REJECTS with the server's
 * message on failure, so a caller can await one and show its own confirmation.
 * `error` holds the last failure for a component that would rather render it than
 * handle it, and `busy` names the action in flight so a bar can disable only the
 * button that was clicked rather than freezing all of them.
 */
export function usePrActions(ref: RepoRef, number: number) {
  const invalidate = useInvalidatePr(ref)
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<Error | null>(null)

  const run = useCallback(
    async <T>(action: string, fn: () => Promise<T>): Promise<T | null> => {
      setBusy(action)
      setError(null)
      try {
        const result = await fn()
        invalidate([number], action)
        return result
      } catch (e) {
        setError(e as Error)
        return null
      } finally {
        setBusy(null)
      }
    },
    [invalidate, number],
  )

  return {
    busy,
    error,
    clearError: useCallback(() => setError(null), []),

    close: useCallback(
      () => run(PR_ACTION.close, () => issueRadarApi.setPrState(ref, number, 'closed')),
      [run, ref, number],
    ),
    reopen: useCallback(
      () => run(PR_ACTION.reopen, () => issueRadarApi.setPrState(ref, number, 'open')),
      [run, ref, number],
    ),
    // Both review verbs take the head sha the caller RENDERED, never a default: a
    // review is a verdict on a revision, and the server refuses one that does not
    // name its commit.
    approve: useCallback(
      (headSha: string, body?: string) =>
        run(PR_ACTION.approve, () =>
          issueRadarApi.submitPrReview(ref, number, REVIEW_EVENT.approve, body, headSha)),
      [run, ref, number],
    ),
    requestChanges: useCallback(
      (headSha: string, body: string) =>
        run(PR_ACTION.requestChanges, () =>
          issueRadarApi.submitPrReview(ref, number, REVIEW_EVENT.requestChanges, body, headSha)),
      [run, ref, number],
    ),
    comment: useCallback(
      (body: string) =>
        run(PR_ACTION.comment, () => issueRadarApi.addPrComment(ref, number, body)),
      [run, ref, number],
    ),
    merge: useCallback(
      // `headSha` is threaded from the caller, never defaulted: the whole point is
      // that it names the commit the UI actually showed.
      (headSha: string, method: MergeMethod = 'SQUASH') =>
        run(PR_ACTION.merge, () => issueRadarApi.mergePr(ref, number, headSha, method)),
      [run, ref, number],
    ),
    setAutoMerge: useCallback(
      (enabled: boolean, method: MergeMethod = 'SQUASH') =>
        run(enabled ? PR_ACTION.autoMerge : PR_ACTION.cancelAutoMerge, () =>
          issueRadarApi.setPrAutoMerge(ref, number, enabled, method)),
      [run, ref, number],
    ),
    // The run actions carry the run id in their `busy` token, so a bar with several
    // runs spins only the row that was clicked.
    cancelRun: useCallback(
      (runId: number) =>
        run(`${PR_ACTION.cancelRun}:${runId}`, () =>
          issueRadarApi.pullRunAction(ref, number, runId, 'cancel')),
      [run, ref, number],
    ),
    rerunRun: useCallback(
      (runId: number, failedOnly = false) =>
        run(`${PR_ACTION.rerunRun}:${runId}`, () =>
          issueRadarApi.pullRunAction(ref, number, runId, 'rerun', failedOnly)),
      [run, ref, number],
    ),
  }
}

/** One row's result in a sequential merge run. */
export interface SequentialMergeRow {
  number: number
  status: 'merged' | 'failed'
  error?: string
}

/**
 * Merge several READY pull requests, one at a time, through the per-PR route.
 *
 * This deliberately does NOT go through `/pulls/bulk`. `merge` is absent from the
 * server's `_BULK_PR_ACTIONS` on purpose — it is irreversible, and the spec's rule 3
 * keeps 50-from-one-click off the bulk endpoint. So each merge here is the ordinary
 * per-PR `/pull/merge` call, which means every safety property of that route still
 * holds per row: the sha PIN (a push landing mid-run is refused, not merged), the
 * server-side `_MERGE_ALLOWED_STATES` re-read, the permission gate, and the SEL audit.
 *
 * Understand the trade this makes, because it is real: it reproduces bulk merge's blast
 * radius without the bulk endpoint's server-side cap. The mitigations are that it is
 * offered ONLY for rows the provider already reports as mergeable-now, it requires a
 * typed confirmation naming the count, and it stops at the first failure.
 *
 * **Sequential, and stops on the first failure.** Not just for the shared rate limit:
 * merging changes the base branch, so PR #2's mergeability is a function of #1 having
 * landed. Firing them together would race, and continuing past a failure would keep
 * merging onto a base whose state has already diverged from what was reviewed. A
 * partial run is reported per row so the user knows exactly where it stopped.
 */
export function useSequentialMerge(ref: RepoRef) {
  const invalidate = useInvalidatePr(ref)
  const [busy, setBusy] = useState(false)
  const [rows, setRows] = useState<SequentialMergeRow[]>([])
  const [running, setRunning] = useState<number | null>(null)
  // Set by `abort()` and checked before EACH row. A merge already in flight cannot be
  // recalled — the request is with the provider — but every row after it can still be
  // spared, and on an irreversible mass action that is the difference between Cancel
  // meaning something and meaning nothing.
  const aborted = useRef(false)
  // Guards against a second concurrent run: the Apply button disables on `busy`, but the
  // confirm input's Enter handler is a second entry point, and two loops advancing
  // independently would defeat stop-on-first-failure and merge a row twice. A ref, not
  // state, because the check has to see the write from the same tick.
  const inFlight = useRef(false)

  const mergeAll = useCallback(
    async (
      // Each entry carries the sha its row was RENDERED at — never re-read at submit
      // time. Same rule as bulk approve: re-reading lets a force-push landing in the
      // window re-point the action at an unreviewed commit, and the server-side pin
      // cannot catch that because the request would carry the new sha.
      targets: Array<{ number: number; headSha: string }>,
      opts: {
        /** Called with each number as it merges, BEFORE that row's caches are
         * invalidated, so a caller that has to exempt the row from its own
         * selection bookkeeping has recorded it before the refetch drops it. */
        onMerged?: (number: number) => void
        /** Called with each number as the loop REACHES it, before the request goes out.
         * Lets a caller track what the run still owes: a row that has been attempted is
         * no longer pending whether it merged or was refused. */
        onAttempt?: (number: number) => void
        /** Asked before each row, so a row the user has DESELECTED since the
         * confirmation is skipped rather than merged.
         *
         * The target set is frozen on purpose (a poll must not change what executes), but
         * an operator unticking a queued PR is withdrawing consent for that PR, and the
         * frozen set cannot represent that on its own. Returning false skips ONE row and
         * lets the run continue: this is a per-row veto, not `abort`, which stops
         * everything and is what Cancel means. */
        stillWanted?: (number: number) => boolean
        method?: MergeMethod
      } = {},
    ): Promise<SequentialMergeRow[]> => {
      const { onMerged, onAttempt, stillWanted, method = MERGE_METHOD.squash } = opts
      // A second concurrent run would merge a row twice and break stop-on-first-failure.
      if (!targets.length || inFlight.current) return []
      inFlight.current = true
      aborted.current = false
      setBusy(true)
      setRows([])
      const done: SequentialMergeRow[] = []
      try {
        for (const { number, headSha } of targets) {
          // Checked per row, so Cancel spares everything not yet sent.
          if (aborted.current) break
          // Deselected since the confirmation: skip this ONE row and carry on. Recorded
          // as attempted first, so the caller stops counting it as owed.
          if (stillWanted && !stillWanted(number)) {
            onAttempt?.(number)
            continue
          }
          setRunning(number)
          // Before the request: this row is no longer owed, whichever way it goes.
          onAttempt?.(number)
          try {
            await issueRadarApi.mergePr(ref, number, headSha, method)
            done.push({ number, status: 'merged' })
            // BEFORE the invalidation, which is what makes the row leave the rendered
            // list: the caller uses this to exempt the row from its own selection
            // bookkeeping, and doing it after leaves a window where the row has already
            // dropped out but is not yet recorded as this run's doing.
            onMerged?.(number)
            // Per row, so a long run shows progress instead of jumping at the end.
            invalidate([number], PR_ACTION.merge)
            setRows([...done])
          } catch (e) {
            done.push({ number, status: 'failed', error: (e as Error).message })
            setRows([...done])
            // STOP. Every remaining PR would merge onto a base that no longer matches
            // what the run was planned against.
            break
          }
        }
        return done
      } finally {
        setRunning(null)
        setBusy(false)
        inFlight.current = false
      }
    },
    [ref, invalidate],
  )

  return {
    mergeAll,
    busy,
    /** The PR currently being merged, for a per-row spinner. */
    running,
    rows,
    /** Stop before the next row. The in-flight merge cannot be recalled. */
    abort: useCallback(() => { aborted.current = true }, []),
    /** Drop the finished run's report WITHOUT aborting anything.
     *
     * Split from `reset` because conflating the two made a selection change kill a
     * live run: the run's own per-row list invalidation drops merged rows out of the
     * rendered list, which changes the selection, which fired the bar's
     * selection-reset effect, which called `reset`, so a 3+ row run aborted itself
     * partway with no refusal to show for it. A caller that only wants to clear
     * stale UI must not be able to stop a merge in flight. */
    clearReport: useCallback(() => { setRows([]) }, []),
    reset: useCallback(() => { aborted.current = true; setRows([]) }, []),
  }
}

/**
 * The bulk action for a set of selected pull requests.
 *
 * Resolves with a {@link BulkOutcome} even when some rows failed — partial failure
 * is the expected case, not an exception, and the list bar reports it per PR. Only
 * a request that failed OUTRIGHT (bad input, no access to the repo, the gateway
 * down) rejects, because then nothing was applied.
 */
export function useBulkPrAction(ref: RepoRef, chunkSize = DEFAULT_BULK_CHUNK) {
  const invalidate = useInvalidatePr(ref)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Error | null>(null)
  const [outcome, setOutcome] = useState<BulkOutcome | null>(null)

  const apply = useCallback(
    async (
      numbers: number[], action: BulkPrAction,
      // `headShas` is keyed by PR NUMBER and is REQUIRED for `approve` (the server
      // rejects a missing or partial map). Sliced per chunk below so each request
      // carries only its own rows' shas.
      opts?: { body?: string; method?: MergeMethod; headShas?: Record<string, string> },
    ): Promise<BulkOutcome | null> => {
      if (!numbers.length) return null
      setBusy(true)
      setError(null)
      setOutcome(null)
      // Accumulated OUTSIDE the try, so a throw part-way through a multi-chunk run
      // does not discard the chunks that already landed. Returning null there told
      // the caller "nothing happened" while earlier writes were real — and since the
      // caller unticks by `applied`, every succeeded row stayed selected and a retry
      // re-applied to it (a second visible comment, for the `comment` verb).
      const applied: number[] = []
      const failed: Array<{ number: number; error: string }> = []
      try {
        // CHUNKED on the SERVER's own cap, which the pulls response publishes. The
        // server rejects a batch over `_BULK_PR_MAX` outright, so an unchunked
        // request meant "select all" on a repo with more open PRs than the cap was a
        // flat 400 with nothing applied. Reading the cap from the response rather
        // than hardcoding it is the same rule the Tagging view follows: a client-side
        // copy silently breaks the day the backend cap changes.
        const size = Math.max(1, chunkSize)
        for (let i = 0; i < numbers.length; i += size) {
          const slice = numbers.slice(i, i + size)
          // Only this chunk's shas: the server requires one for every number IN the
          // request, and sending the whole map would be sending shas for PRs this
          // request does not touch.
          const sliceShas = opts?.headShas
            ? Object.fromEntries(
              slice.map((n) => [String(n), opts.headShas?.[String(n)] ?? '']),
            )
            : undefined
          const res: BulkPrResponse = await issueRadarApi.bulkPrAction(
            ref, slice, action, { ...opts, headShas: sliceShas },
          )
          const sliceApplied = res.applied.map((row) => row.number)
          // Invalidate per chunk, so a long run reflects progress rather than
          // waiting for the whole batch. Only the PRs that actually changed: a
          // failed row still holds its pre-action state.
          invalidate(sliceApplied, action)
          applied.push(...sliceApplied)
          failed.push(...(res.failed ?? []))
        }
        const result: BulkOutcome = { action, applied, failed }
        setOutcome(result)
        return result
      } catch (e) {
        setError(e as Error)
        // A partial run is still an outcome. Report what DID land (plus the error
        // above) rather than null, so the caller unticks those rows and the retry
        // covers only what is genuinely outstanding.
        if (applied.length === 0 && failed.length === 0) return null
        const partial: BulkOutcome = { action, applied, failed }
        setOutcome(partial)
        return partial
      } finally {
        setBusy(false)
      }
    },
    [ref, invalidate, chunkSize],
  )

  return {
    apply,
    busy,
    error,
    outcome,
    reset: useCallback(() => { setOutcome(null); setError(null) }, []),
  }
}
