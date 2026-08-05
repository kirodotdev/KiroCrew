// The bulk-action bar that appears above the PR list once rows are ticked.
//
// Mass triage: approve, comment, close/reopen, or arm the provider's own
// auto-merge across a selection. It is the same action set as the per-PR bar minus
// "request changes", which is per-PR only — a mass change-request without per-PR
// reasoning is not feedback anyone can act on, and the server's allowlist agrees.
//
// Two properties worth stating, because they are what make a mass mutation safe to
// offer at all:
//
//  * **A destructive or hard-to-undo action requires a typed confirmation.**
//    Closing N pull requests is the one action here whose blast radius is real
//    (each close is a separate notification to a separate author), so it arms only
//    when the user types the confirm token — the same pattern SchedulePage uses for
//    bulk delete. Approving is reversible, commenting is additive, and arming
//    auto-merge is reversible AND leaves the provider deciding each one, so those
//    apply directly. Note the last claim is only true because the GitLab client
//    refuses to arm when no pipeline is in flight: there, the arm flag rides on the
//    merge endpoint and would otherwise merge immediately, which is exactly the
//    irreversible mass action this confirmation policy exists to prevent. Merging
//    itself is absent from this bar for the same reason.
//  * **Partial failure is REPORTED, not thrown away.** A batch where one PR was
//    locked and nine succeeded shows exactly that, per PR. Silently reporting "done"
//    would leave the user believing a write happened that did not.
import { useEffect, useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Check, CircleSlash, CircleDot, MessageSquarePlus, GitMerge, X, Loader2, AlertTriangle,
} from 'lucide-react'
import { Btn } from '../../../components/ui'
import {
  useBulkPrAction, useSequentialMerge, PR_ACTION,
  canArmAutoMerge as rowCanArm, canMergeNow as rowCanMergeNow,
} from '../lib/prActions'
import {
  BULK_PR_CLOSE_TOKEN as CLOSE_TOKEN, MERGE_READY_ACTION, SEQUENTIAL_MERGE_TOKEN,
  SEQUENTIAL_MERGE_METHOD,
} from '../lib/wireValues'
import { fmtList } from '../../../i18n/format'
import { useIssueRadar } from '../context'
import { providerTerms, isGitlab, repoScopeKey } from '../lib/links'
import type { BulkPrAction } from '../api'

import { i18nT } from '../../../i18n/t'

// The confirmation TOKENS and the merge pseudo-action are protocol values, not copy:
// a translated token makes the action impossible to complete. They live in
// `lib/wireValues.ts` with every other by-value string on this surface, and are
// re-exported here because that is where callers (and the i18n guard test) import them.
export { BULK_PR_CLOSE_TOKEN, SEQUENTIAL_MERGE_TOKEN } from '../lib/wireValues'



// The confirmation's two explanatory lines, referenced by the input's
// `aria-describedby`: a screen-reader user tabbing to the field would otherwise hear only
// "type the confirmation word" and never the set being merged or the method.
const CONFIRM_WARNING_ID = 'pr-bulk-confirm-warning'
const CONFIRM_TARGETS_ID = 'pr-bulk-confirm-targets'

/** Which actions need a body, and which need the typed confirmation. */
const NEEDS_BODY = new Set<BulkPrAction>(['comment'])
const NEEDS_CONFIRM = new Set<BulkPrAction>(['close'])



export default function PrBulkBar() {
  const {
    active, canWrite, checkedPulls, clearCheckedPulls, toggleAllPullsChecked, sortedPulls,
    togglePullChecked, prBulkMax,
  } = useIssueRadar()
  const terms = providerTerms(active)
  // See PrActionsBar: GitLab cannot arm a deferred merge safely, so the client
  // refuses it and these two buttons would only ever error there.
  const canArmAutoMerge = !isGitlab(active)
  // Chunked on the SERVER's published cap: a selection larger than one request may
  // hold was a flat 400 with nothing applied.
  const bulk = useBulkPrAction(active, prBulkMax)
  // Merging the ready rows does NOT go through the bulk endpoint — `merge` is
  // deliberately absent from the server's allowlist — so it has its own driver that
  // walks the per-PR route one row at a time. See `useSequentialMerge`.
  const seq = useSequentialMerge(active)

  // The action the user picked and is now completing (typing a body, or the
  // confirmation). `null` means the bar is showing its buttons.
  const [pending, setPending] = useState<BulkPrAction | typeof MERGE_READY_ACTION | null>(null)
  // The exact rows a merge confirmation was ARMED against, frozen when it opened.
  // An irreversible action must apply to the set the user was shown — see
  // `runSequentialMerge`.
  const [armedMerge, setArmedMerge] = useState<Array<{ number: number; headSha: string }>>([])
  // Held in a ref so the selection-change effect can clear the run report while staying
  // keyed on the selection ALONE. Depending on `seq.clearReport` directly would re-key
  // the effect on an unstable identity and re-fire it on unrelated renders, wiping a
  // typed confirmation mid-entry.
  //
  // `clearReport`, NOT `reset`: `reset` also aborts, and this effect fires on selection
  // changes the RUN ITSELF causes (see `mergedByRun`), so the abort reached a merge loop
  // that was still going.
  const seqClearReportRef = useRef(seq.clearReport)
  seqClearReportRef.current = seq.clearReport
  // Read by the disarm effect without being a dependency of it, for the same reason as
  // every other ref here: that effect must re-key on the selection alone.
  const seqBusyRef = useRef(seq.busy)
  seqBusyRef.current = seq.busy
  // The numbers THIS RUN merged, so the selection changes it causes are not mistaken for
  // the user reselecting.
  //
  // A successful merge changes the selection twice over: the per-row cache invalidation
  // drops the row out of the refetched list (so it leaves `numbers`, which is intersected
  // with what is rendered), and `runSequentialMerge` unticks it afterwards. Both are the
  // run's own bookkeeping, but the reset effect below could not tell them from a user
  // click, so a 3+ row run aborted itself midway, and the per-row report the whole
  // feature is built around was wiped the moment it was complete.
  const mergedByRun = useRef<Set<number>>(new Set())
  // Belt and braces on the above: the exemption is meaningless outside the repo it was
  // recorded in (PR numbers are per-repo), and this component is NOT remounted by a repo
  // switch, so the ref would otherwise carry repo A's merged numbers into repo B.
  const scopeKey = repoScopeKey(active)
  const lastScope = useRef(scopeKey)
  if (lastScope.current !== scopeKey) {
    lastScope.current = scopeKey
    mergedByRun.current = new Set()
  }
  // Whether a row is STILL ticked, read at the moment the run reaches it.
  //
  // The run walks the frozen `armedMerge` so a poll cannot change what executes, but an
  // operator unticking a queued PR mid-run is withdrawing consent for that PR and the
  // frozen set cannot represent that. A ref-held predicate keeps this out of the
  // selection effect's dependencies (keying that effect on anything but the selection
  // re-fires it on unrelated renders and wipes a confirmation mid-entry) while still
  // reading the LIVE ticked set each time the loop asks.
  //
  // A per-row veto, not an abort: the rows after it were not withdrawn, and `abort` is
  // what Cancel means.
  const stillTicked = useRef<(n: number) => boolean>(() => true)
  stillTicked.current = (n: number) => checkedPulls.has(n)
  // How many rows the LAST run started with, so the "N not attempted" line has a fixed
  // denominator. `armedMerge` is cleared when the run ends and the live target list
  // shrinks as rows merge, so neither can measure the run after the fact.
  const [mergeRunSize, setMergeRunSize] = useState(0)
  const [text, setText] = useState('')
  const [confirmText, setConfirmText] = useState('')

  const visible = sortedPulls.map((p) => p.number)
  // Intersected with what is RENDERED, not just with what was ticked: a row can
  // leave the view after it was selected (a search, a label or person filter, a
  // draft toggle) and it would otherwise still be acted on — breaking the "you can
  // only mass-act on what you can see" rule the checkbox is offered under.
  const numbers = visible.filter((n) => checkedPulls.has(n))
  const count = numbers.length
  const allTicked = visible.length > 0 && visible.every((n) => checkedPulls.has(n))

  // The head commit each selected row carried WHEN IT WAS TICKED, not when Apply was
  // pressed.
  //
  // The list query polls, so reading `p.head_sha` at submit time meant a force-push
  // landing between the tick and the click silently re-pointed the approval at the new
  // head — the exact defect the server-side pin exists to prevent, reintroduced on the
  // client where the pin cannot see it (the request would carry the NEW sha, so the
  // server has nothing to refuse). Snapshotting at tick time keeps the submitted sha
  // equal to the one that was on screen, which turns the race into the server's 422 /
  // 409 refusal instead of a recorded verdict on unseen code.
  //
  // A ref, not state: this is a record of what was observed, and re-rendering on it
  // would be pointless — nothing displays it.
  //
  // Seeded during RENDER rather than in an effect. An effect runs after the first paint,
  // so a bar that mounts with rows already ticked (a re-render, or a selection made
  // before this component existed) would have an empty map on that first pass and offer
  // no approve at all. Writing a ref during render is safe here because it is
  // idempotent and order-independent: each row's first observed sha wins, and a repeat
  // render observes the same value.
  const shaAtTick = useRef<Map<number, string>>(new Map())
  const seen = shaAtTick.current
  for (const p of sortedPulls) {
    // First observation wins — that is the whole point. A later poll carrying a
    // force-pushed head must NOT replace the sha that was on screen when the row was
    // ticked, or the approval silently re-targets and the server has nothing to refuse.
    if (checkedPulls.has(p.number) && p.head_sha && !seen.has(p.number)) {
      seen.set(p.number, p.head_sha)
    }
  }
  // Forget rows that left the selection, so a re-tick after a real refresh picks up the
  // sha showing at THAT moment rather than a stale one from an earlier tick.
  for (const n of [...seen.keys()]) if (!checkedPulls.has(n)) seen.delete(n)

  const headShas: Record<string, string> = {}
  for (const n of checkedPulls) {
    const sha = seen.get(n)
    if (sha) headShas[String(n)] = sha
  }
  // Approve is only offered when EVERY selected row has one. A partial map is rejected
  // by the server outright, and silently approving the subset that happens to have a
  // sha would apply an action to fewer PRs than the button's count claims.
  const canBulkApprove = count > 0 && numbers.every((n) => Boolean(headShas[String(n)]))

  // Split the selection by what the provider will actually ACCEPT.
  //
  // This is the fix for the defect that motivated all of it: the bar used to offer
  // auto-merge for every ticked row, and GitHub refused each one that was already
  // mergeable ("Pull request is in clean status") or already merged ("Pull request is
  // already merged") — one failure per row, from a single click. The rows carry
  // `mergeable_state` now, so the two verbs can be offered to the rows they apply to.
  //
  // Rows whose readiness is UNKNOWN fall into NEITHER bucket. That is deliberate:
  // GitHub computes mergeability asynchronously, and a gate that cannot tell must
  // refuse rather than guess (see `canArmAutoMerge`).
  const selectedRows = sortedPulls.filter((p) => checkedPulls.has(p.number))
  const armableRows = selectedRows.filter(rowCanArm)
  // Ready to merge NOW — and only those with a snapshotted sha, since the merge is
  // pinned to the commit the row was rendered at exactly as an approval is. Both
  // buckets go through the SHARED predicates so they cannot diverge on a lifecycle
  // field (an earlier revision checked `draft` in one and not the other).
  const readyRows = selectedRows.filter(
    (p) => rowCanMergeNow(p) && Boolean(seen.get(p.number)),
  )
  // Capped at the SERVER's published bulk cap. The sequential merge does not use the
  // bulk endpoint, so nothing else would bound it — and spec rule 3's reasoning ("50
  // from one click is a blast radius no confirmation makes reasonable") applies to a
  // loop just as much as to a batch. Anything beyond the cap needs a second click.
  const mergeTargets = readyRows.slice(0, prBulkMax).map((p) => ({
    number: p.number,
    headSha: seen.get(p.number) as string,
  }))

  // TWO keys, because the two things this effect used to do want different answers.
  //
  // `selectionKey` is the literal ticked set, and it disarms an in-progress action: a
  // confirmation or a typed body must never survive a change to what it applies to. Keyed
  // on IDENTITY, not size, since a same-size swap (7,8 -> 7,9) otherwise left Apply armed
  // and closed a PR the user had never confirmed.
  //
  // It must stay the LITERAL set. Folding the run's merged numbers in here made the
  // exemption able to mask an ADDITION too, not just the run's own disappearance: with
  // `mergedByRun = {7}` (recorded in another repo, since nothing resets it on a repo
  // switch and this component is not remounted by one), ticking a live #7 left the key
  // unchanged at `7,9`, so a typed close confirmation stayed armed and Apply closed it.
  const selectionKey = numbers.join(',')
  // `reportKey` decides only whether the RUN REPORT is stale, and that is where the run's
  // own churn has to be invisible: a merged row leaves the list (its invalidation
  // refetches) and then unticks itself, and neither is a user reselection. Merged numbers
  // are ADDED BACK rather than filtered out, which is the subtlety: a merged row WAS in
  // the key before it merged, so removing it changes the key just as much as its
  // disappearance did (`7,8` -> `8`). Re-adding reproduces the pre-run key exactly.
  // Sorted because the union's insertion order is not the render order.
  const reportKey = [...new Set([...numbers, ...mergedByRun.current])]
    .sort((a, b) => a - b)
    .join(',')
  useEffect(() => {
    // NOT while a sequential merge is running. Cancel lives in this composer, and the
    // first merged row changes the ticked set (its invalidation refetches, so the row
    // leaves the list), which would clear `pending` and take the only control that stops
    // the remaining rows off screen mid-run. An irreversible loop has to stay
    // interruptible for as long as it is running; the run ends by clearing these itself.
    if (seqBusyRef.current) return
    setPending(null)
    setText('')
    setConfirmText('')
    setArmedMerge([])
  }, [selectionKey])
  useEffect(() => {
    // The previous run's per-row report names PR numbers and counts "remaining" against
    // the set it ran on, so carrying it into a different selection reports another
    // selection's failures as if they were this one's. Clearing it does NOT abort: this
    // fires on the run's own churn as well, and a caller that only tidies the UI must not
    // be able to stop a merge in flight.
    seqClearReportRef.current()
  }, [reportKey])

  // The bar stays mounted while it has an OUTCOME to report, even once the
  // selection is empty. Returning null on `count === 0` alone meant a fully clean
  // run cleared the selection and unmounted the bar before "Applied to N" could
  // paint — the user got no confirmation at all, and the success copy was
  // unreachable in every language.
  if (!canWrite || (count === 0 && !bulk.outcome && !bulk.error && !seq.rows.length)) return null

  const reset = () => {
    setPending(null)
    setText('')
    setConfirmText('')
    setArmedMerge([])
    bulk.reset()
    seq.reset()
  }

  /** Merge the ARMED set, one at a time, stopping at the first refusal.
   *
   * Runs `armedMerge` — the set frozen when the confirmation opened — never the live
   * `mergeTargets`. The list POLLS, so a readiness change landing between "type the
   * token" and "press Apply" would otherwise change what gets merged: a warning that
   * said "1 pull request" could execute six, because a cold read reports `unknown`
   * (neither bucket) and resolves to `clean` (ready) a moment later. Same reasoning as
   * `shaAtTick` one screen up — for an irreversible action the user must merge exactly
   * the set they were shown. */
  const runSequentialMerge = async () => {
    if (!armedMerge.length || seq.busy) return
    setMergeRunSize(armedMerge.length)
    // A fresh run owns a fresh exemption set. Carrying the previous run's numbers would
    // make re-ticking one of them look like the run's own bookkeeping and suppress the
    // reset a real reselection must perform.
    mergedByRun.current = new Set()
    const done = await seq.mergeAll(armedMerge, {
        // Recorded BEFORE react-query refetches, so the row is already exempt from
        // `selectionKey` by the time it drops out of the list.
        onMerged: (number) => mergedByRun.current.add(number),
      // Asked per row against the LIVE selection, so a PR the user deselected while the
      // run was working is skipped instead of merged from the frozen set.
      stillWanted: (number) => stillTicked.current(number),
      // The SAME symbol the confirmation names, so the copy cannot drift from the
      // method actually sent.
      method: SEQUENTIAL_MERGE_METHOD,
    })
    // Untick only what actually merged, so a run that stopped early leaves the
    // outstanding rows selected and a retry covers exactly them.
    for (const row of done) if (row.status === 'merged') togglePullChecked(row.number)
    setPending(null)
    setConfirmText('')
    setArmedMerge([])
  }

  const apply = async (action: BulkPrAction) => {
    // Arming is sent ONLY for the rows it can apply to. Sending the whole selection is
    // what produced a wall of "Pull request is in clean status" / "is already merged"
    // failures from one click: the provider refuses to arm a PR that is already
    // mergeable or already merged, and every such row came back as its own error.
    const targets = action === PR_ACTION.autoMerge
      ? armableRows.map((p) => p.number)
      : numbers
    if (!targets.length) return
    const result = await bulk.apply(targets, action, {
      body: text.trim() || undefined,
      // Only for the pinned verb: sending shas for `close` would be sending data the
      // server has no field for, and the hook slices this map per chunk.
      headShas: action === 'approve' ? headShas : undefined,
    })
    // Untick exactly the rows that SUCCEEDED, leaving the failures selected for a
    // retry. Keeping the whole selection on a partial run made the retry re-apply to
    // the rows that already worked — which for `comment` posts a second copy, and is
    // the one action here where a repeat is visible to everyone on the PR.
    if (result) {
      for (const n of result.applied) togglePullChecked(n)
    }
    setPending(null)
    setText('')
    setConfirmText('')
  }

  const start = (action: BulkPrAction | typeof MERGE_READY_ACTION) => {
    // Merging always needs the typed confirmation — it is the one action here that
    // cannot be undone. The target set is FROZEN here, at the moment the warning is
    // rendered, so the count the user reads is the count that executes.
    if (action === MERGE_READY_ACTION) {
      setArmedMerge(mergeTargets)
      setPending(action)
      return
    }
    if (NEEDS_BODY.has(action) || NEEDS_CONFIRM.has(action)) {
      setPending(action)
      return
    }
    apply(action)
  }

  const isMergePending = pending === MERGE_READY_ACTION
  // Each destructive action has its OWN token, so typing one cannot arm the other.
  const requiredToken = isMergePending ? SEQUENTIAL_MERGE_TOKEN : CLOSE_TOKEN
  const confirmArmed = confirmText.trim().toLowerCase() === requiredToken
  const bodyArmed = !pending || isMergePending || !NEEDS_BODY.has(pending) || Boolean(text.trim())
  const needsConfirm = pending
    ? isMergePending || NEEDS_CONFIRM.has(pending)
    : false
  // A merge confirmation over zero rows is never submittable: `armedMerge` can be
  // emptied by the run itself, and an enabled "Apply to 0" over "This merges 0 pull
  // requests" is an armed button on nonsense copy.
  const hasTargets = !isMergePending || armedMerge.length > 0
  const canSubmit = pending
    ? bodyArmed && hasTargets && (!needsConfirm || confirmArmed)
    : false

  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      className="mx-2 mb-1.5 rounded-lg border border-accent/40 bg-card p-2"
      role="region"
      aria-label={i18nT('apps.issueRadar.components.prBulkBar.bulk_actions')}
    >
      <div className="flex items-center gap-2 flex-wrap">
        {count > 0 && (
          <span className="text-[12px] font-medium text-text-strong">
            {i18nT('apps.issueRadar.components.prBulkBar.selected', { count })}
          </span>
        )}
        {count > 0 && (
          <Btn
            onClick={toggleAllPullsChecked}
            className="px-1.5 py-0.5 text-[11.5px]"
            title={i18nT('apps.issueRadar.components.prBulkBar.select_all_visible')}
          >
            {allTicked
              ? i18nT('apps.issueRadar.components.prBulkBar.deselect_all')
              : i18nT('apps.issueRadar.components.prBulkBar.select_all')}
          </Btn>
        )}
        <Btn
          onClick={() => { clearCheckedPulls(); reset() }}
          aria-label={i18nT('apps.issueRadar.components.prBulkBar.clear_selection')}
          className="px-1.5 py-0.5"
        >
          <X className="lucide-inline" />
        </Btn>
      </div>

      {/* The action row, or the completion step for the action being armed. With
          nothing selected the bar is only reporting a finished run, so neither
          is shown — offering an action over zero rows would be a dead button. */}
      {count === 0 ? null : !pending ? (
        <div className="mt-2 flex items-center gap-1.5 flex-wrap">
          <Btn
            onClick={() => start('approve')}
            // Disabled, not hidden, when a selected row has no head commit: the button
            // moving would make the whole row jump, and the tooltip can say why.
            disabled={bulk.busy || !canBulkApprove}
            title={canBulkApprove
              ? i18nT('apps.issueRadar.components.prBulkBar.approve_hint', { subject: terms.changeRequestPlural })
              : i18nT('apps.issueRadar.components.prBulkBar.approve_needs_commit')}
          >
            {bulk.busy ? <Loader2 className="lucide-inline animate-spin" /> : <Check className="lucide-inline" />}
            {i18nT('apps.issueRadar.components.prBulkBar.approve')}
          </Btn>
          <Btn
            onClick={() => start('comment')}
            disabled={bulk.busy}
            title={i18nT('apps.issueRadar.components.prBulkBar.comment_hint', { subject: terms.changeRequestPlural })}
          >
            <MessageSquarePlus className="lucide-inline" />
            {i18nT('apps.issueRadar.components.prBulkBar.comment')}
          </Btn>
          {/* Merge the rows the provider already reports as mergeable NOW. Absent from
              the bulk endpoint by design, so this drives the per-PR route once per row
              (see `useSequentialMerge`) — every merge stays pinned to its reviewed
              commit. Rendered only when such rows are actually selected: an always-on
              button would be dead for most selections.

              OUTSIDE the `canArmAutoMerge` gate on purpose. That flag is about GitLab's
              inability to ARM a deferred merge safely; `/pull/merge` works there, and
              `MERGE_READY_STATES` carries GitLab's own `mergeable`. Nesting this inside
              it would withhold the merge path on GitLab for an unrelated reason. */}
          {mergeTargets.length > 0 && (
            <Btn
              onClick={() => start(MERGE_READY_ACTION)}
              disabled={bulk.busy || seq.busy}
              title={i18nT('apps.issueRadar.components.prBulkBar.merge_ready_hint')}
            >
              {seq.busy
                ? <Loader2 className="lucide-inline animate-spin" />
                : <GitMerge className="lucide-inline" />}
              {i18nT('apps.issueRadar.components.prBulkBar.merge_ready', {
                count: mergeTargets.length,
              })}
            </Btn>
          )}
          {canArmAutoMerge && (<>
          <Btn
            onClick={() => start(PR_ACTION.autoMerge)}
            // Disabled when NO selected row can be armed — every one is either already
            // mergeable (the provider refuses: "in clean status"), already merged, or of
            // unknown readiness. This is the defect that motivated the change: the
            // button used to fire regardless and collect one refusal per row.
            disabled={bulk.busy || seq.busy || armableRows.length === 0}
            // Says what it does, because the difference from "merge now" is the
            // entire point: the provider still decides.
            title={armableRows.length > 0
              ? i18nT('apps.issueRadar.components.prBulkBar.auto_merge_hint')
              : i18nT('apps.issueRadar.components.prBulkBar.auto_merge_none_pending')}
          >
            <GitMerge className="lucide-inline" />
            {armableRows.length > 0 && armableRows.length !== count
              ? i18nT('apps.issueRadar.components.prBulkBar.auto_merge_n', {
                count: armableRows.length,
              })
              : i18nT('apps.issueRadar.components.prBulkBar.auto_merge')}
          </Btn>
          <Btn
            onClick={() => start(PR_ACTION.cancelAutoMerge)}
            disabled={bulk.busy}
            title={i18nT('apps.issueRadar.components.prBulkBar.cancel_auto_merge_hint')}
          >
            <CircleSlash className="lucide-inline" />
            {i18nT('apps.issueRadar.components.prBulkBar.cancel_auto_merge')}
          </Btn>
          </>)}
          <Btn
            onClick={() => start('reopen')}
            disabled={bulk.busy}
            title={i18nT('apps.issueRadar.components.prBulkBar.reopen_hint', { subject: terms.changeRequestPlural })}
          >
            <CircleDot className="lucide-inline" />
            {i18nT('apps.issueRadar.components.prBulkBar.reopen')}
          </Btn>
          <Btn
            danger
            onClick={() => start('close')}
            disabled={bulk.busy}
            title={i18nT('apps.issueRadar.components.prBulkBar.close_hint', { subject: terms.changeRequestPlural })}
          >
            <CircleSlash className="lucide-inline" />
            {i18nT('apps.issueRadar.components.prBulkBar.close')}
          </Btn>
        </div>
      ) : (
        <div className="mt-2">
          {!isMergePending && NEEDS_BODY.has(pending) && (
            <textarea
              value={text}
              onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') {
                  // Stop the list's window-level Escape handler: it clears the whole
                  // selection, and here the user only means "close this composer".
                  e.preventDefault()
                  e.stopPropagation()
                  reset()
                }
              }}
              placeholder={i18nT('apps.issueRadar.components.prBulkBar.comment_placeholder')}
              aria-label={i18nT('apps.issueRadar.components.prBulkBar.comment_placeholder')}
              rows={2}
              className="w-full bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[13px] text-text placeholder:text-muted outline-none resize-y transition-colors focus-ring font-body"
            />
          )}
          {needsConfirm && (
            <>
              <div id={CONFIRM_WARNING_ID} className="text-[12px] text-danger mb-1.5">
                {isMergePending
                  // The FROZEN count, matching exactly what Apply will merge.
                  ? i18nT('apps.issueRadar.components.prBulkBar.merge_warning', {
                    count: armedMerge.length,
                  })
                  : i18nT('apps.issueRadar.components.prBulkBar.close_warning', { count })}
              </div>
              {/* The frozen SET, not just its count. An irreversible action should be
                  inspectable before it runs, and a count alone cannot be checked against
                  what the user believes they ticked. Bounded by `prBulkMax`, so this is
                  always a short line. The method is named for the same reason: it is
                  hardcoded, so a merge-commit repo would otherwise discover the squash
                  only afterwards.
                  NOT muted: this is the only line naming WHICH pull requests are about to
                  be merged irreversibly, so it must not read as subordinate to the count
                  above it. It wraps rather than truncating, deliberately - at the cap of
                  50 that is a few lines, and clipping the identifiers would defeat the
                  inspection the line exists for. */}
              {isMergePending && armedMerge.length > 0 && (
                <div id={CONFIRM_TARGETS_ID} className="text-[12px] text-text mb-1.5">
                  {i18nT('apps.issueRadar.components.prBulkBar.merge_targets', {
                    // The PROVIDER's own sigil, like every other reference on this
                    // surface (the run report below renders `{terms.sigil}{number}`).
                    // GitLab writes `!7`, and this path is reachable there: only the two
                    // AUTO-merge buttons are GitLab-gated, not merge-now.
                    //
                    // Raw digits, NOT `fmtNumber`: a PR number is an identifier, so a
                    // grouping separator would both misrender it (`#1,291`) and break
                    // copying it into a search. `fmtList` still supplies the locale's own
                    // enumeration (zh-CN uses `、`, not `, `).
                    numbers: fmtList(armedMerge.map((t) => `${terms.sigil}${t.number}`)),
                    method: SEQUENTIAL_MERGE_METHOD.toLowerCase(),
                  })}
                </div>
              )}
              <input
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    e.preventDefault()
                    e.stopPropagation()  // see the comment on the textarea above
                    reset()
                  }
                  // `seq.busy` / `bulk.busy` are checked here as well as on the Apply
                  // button: this is a SECOND entry point, and without the guard a second
                  // Enter mid-run started a concurrent loop that merged a row twice and
                  // defeated stop-on-first-failure.
                  if (e.key === 'Enter' && confirmArmed && !seq.busy && !bulk.busy) {
                    e.preventDefault()
                    if (isMergePending) runSequentialMerge()
                    else apply(pending)
                  }
                }}
                placeholder={requiredToken}
                aria-label={i18nT('apps.issueRadar.components.prBulkBar.type_to_confirm')}
                // Names the warning AND the target set, so the field is not read in
                // isolation from what it is about to authorize.
                aria-describedby={
                  isMergePending && armedMerge.length > 0
                    ? `${CONFIRM_WARNING_ID} ${CONFIRM_TARGETS_ID}`
                    : CONFIRM_WARNING_ID
                }
                // The confirmation is the last step before an irreversible action, so the
                // caret belongs in it rather than several tab stops away.
                autoFocus
                className="w-full bg-bg-elevated border border-border rounded-md px-2.5 py-1.5 text-[13px] text-text placeholder:text-muted outline-none transition-colors focus-ring font-body"
              />
            </>
          )}
          <div className="mt-1.5 flex items-center gap-1.5">
            <Btn
              primary
              onClick={() => (isMergePending ? runSequentialMerge() : apply(pending))}
              disabled={!canSubmit || bulk.busy || seq.busy}
            >
              {bulk.busy || seq.busy
                ? <Loader2 className="lucide-inline animate-spin" />
                : <Check className="lucide-inline" />}
              {i18nT('apps.issueRadar.components.prBulkBar.apply', {
                count: isMergePending ? armedMerge.length : count,
              })}
            </Btn>
            {/* Cancel ABORTS a run in progress, not just the composer. It cannot recall
                the merge already in flight — that request is with the provider — but it
                spares every row after it, which on an irreversible mass action is the
                difference between Cancel meaning something and meaning nothing. */}
            <Btn onClick={() => { seq.abort(); reset() }}>
              {i18nT('apps.issueRadar.components.prBulkBar.cancel')}
            </Btn>
          </div>
        </div>
      )}

      {/* A request that failed OUTRIGHT — nothing was applied. */}
      {bulk.error && (
        <div className="mt-2 flex items-start gap-1.5 text-[12px] text-danger">
          <AlertTriangle className="lucide-inline flex-shrink-0" />
          <span className="min-w-0 break-words">{bulk.error.message}</span>
        </div>
      )}

      {/* The sequential merge's per-row progress. Named individually and in order,
          because this run STOPS at the first refusal: the user has to be able to see
          exactly which PR blocked it and which ones were never attempted. */}
      <AnimatePresence>
        {seq.rows.length > 0 && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="mt-2 text-[12px] overflow-hidden"
            // The rows stream in one at a time during an irreversible run, so a screen
            // reader has to be told as each lands rather than only on completion.
            aria-live="polite"
          >
            <ul className="space-y-0.5">
              {seq.rows.map((row) => (
                <li
                  key={row.number}
                  className={row.status === 'merged' ? 'text-ok' : 'text-danger'}
                >
                  <span className="break-words">
                    {terms.sigil}{row.number}
                    {row.status === 'merged'
                      ? ` — ${i18nT('apps.issueRadar.components.prBulkBar.merged_ok')}`
                      : ` — ${row.error ?? ''}`}
                  </span>
                </li>
              ))}
              {/* The row currently in flight, so the run shows WHERE it is rather than
                  only a global spinner. */}
              {seq.running !== null && (
                <li className="text-muted">
                  <Loader2 className="lucide-inline animate-spin" />
                  {' '}{terms.sigil}{seq.running}
                </li>
              )}
            </ul>
            {/* Says the run stopped, and how many were left untouched — otherwise a
                halted run is indistinguishable from one that finished.
                Counted against `mergeRunSize`, the size of the set the run STARTED
                with. Measuring against the live target list undercounted (or printed
                nothing at all) on a partial success, because each merged row unticks
                itself and so leaves that list — the denominator moved as the run
                progressed, which is exactly when this line matters most. */}
            {seq.rows.some((r) => r.status === 'failed')
              && mergeRunSize > seq.rows.length && (
              <div className="mt-1 text-muted">
                {i18nT('apps.issueRadar.components.prBulkBar.merge_halted', {
                  count: mergeRunSize - seq.rows.length,
                })}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Per-PR outcome. The failures are named individually so the user knows
          exactly which rows to revisit — a bare count would send them to re-check
          all of them. */}
      <AnimatePresence>
        {bulk.outcome && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="mt-2 text-[12px] overflow-hidden"
          >
            {bulk.outcome.applied.length > 0 && (
              <div className="text-ok">
                {i18nT('apps.issueRadar.components.prBulkBar.applied', {
                  count: bulk.outcome.applied.length,
                })}
              </div>
            )}
            {bulk.outcome.failed.length > 0 && (
              <div className="mt-1 text-danger">
                <div>
                  {i18nT('apps.issueRadar.components.prBulkBar.failed', {
                    count: bulk.outcome.failed.length,
                  })}
                </div>
                <ul className="mt-0.5 space-y-0.5">
                  {bulk.outcome.failed.map((f) => (
                    <li key={f.number} className="break-words">
                      {terms.sigil}{f.number} — {f.error}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  )
}
