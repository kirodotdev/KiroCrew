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
import { useCallback, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import {
  issueRadarApi,
  type BulkPrAction, type BulkPrResponse, type RepoRef,
} from '../api'
import { repoScopeKey } from './links'

/**
 * Fallback chunk size when the server has not published its cap.
 *
 * Only reached by a cached response written before `bulk_max` shipped; deliberately
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
export type MergeMethod = 'MERGE' | 'SQUASH' | 'REBASE'

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
