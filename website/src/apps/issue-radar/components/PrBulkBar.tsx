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
import { useBulkPrAction, PR_ACTION } from '../lib/prActions'
import { useIssueRadar } from '../context'
import { providerTerms, isGitlab } from '../lib/links'
import type { BulkPrAction } from '../api'

import { i18nT } from '../../../i18n/t'

/** The literal the user types to arm a bulk close.
 *
 * A CODE CONSTANT, never a catalog value — translating it would make the action
 * impossible to complete for anyone not typing English (see website/AGENTS.md, and
 * destructiveConfirm.test.ts which pins the same rule for SchedulePage's bulk
 * delete).
 *
 * Deliberately NOT the bare word "close": that is also this bar's own button label,
 * so the confirmation could be satisfied by copying the very button the user just
 * pressed — which is not a deliberate second act. `close prs` shares no value with
 * any label in any of the ten catalogs. */
export const BULK_PR_CLOSE_TOKEN = 'close prs'

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

  // The action the user picked and is now completing (typing a body, or the
  // confirmation). `null` means the bar is showing its buttons.
  const [pending, setPending] = useState<BulkPrAction | null>(null)
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

  // Reset the in-progress action whenever the selection changes.
  //
  // Keyed on the selection's IDENTITY, not its size. Keying on the count let a
  // same-size swap (7,8 -> 7,9) keep an armed confirmation or a typed body: the
  // effect never fired, so Apply stayed enabled and closed a PR the user had never
  // confirmed, or posted prose written about a different set.
  const selectionKey = numbers.join(',')
  useEffect(() => {
    setPending(null)
    setText('')
    setConfirmText('')
  }, [selectionKey])

  // The bar stays mounted while it has an OUTCOME to report, even once the
  // selection is empty. Returning null on `count === 0` alone meant a fully clean
  // run cleared the selection and unmounted the bar before "Applied to N" could
  // paint — the user got no confirmation at all, and the success copy was
  // unreachable in every language.
  if (!canWrite || (count === 0 && !bulk.outcome && !bulk.error)) return null

  const reset = () => {
    setPending(null)
    setText('')
    setConfirmText('')
    bulk.reset()
  }

  const apply = async (action: BulkPrAction) => {
    const result = await bulk.apply(numbers, action, {
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

  const start = (action: BulkPrAction) => {
    if (NEEDS_BODY.has(action) || NEEDS_CONFIRM.has(action)) {
      setPending(action)
      return
    }
    apply(action)
  }

  const confirmArmed = confirmText.trim().toLowerCase() === BULK_PR_CLOSE_TOKEN
  const bodyArmed = !pending || !NEEDS_BODY.has(pending) || Boolean(text.trim())
  const canSubmit = pending
    ? bodyArmed && (!NEEDS_CONFIRM.has(pending) || confirmArmed)
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
          {canArmAutoMerge && (<>
          <Btn
            onClick={() => start(PR_ACTION.autoMerge)}
            disabled={bulk.busy}
            // Says what it does, because the difference from "merge now" is the
            // entire point: the provider still decides.
            title={i18nT('apps.issueRadar.components.prBulkBar.auto_merge_hint')}
          >
            <GitMerge className="lucide-inline" />
            {i18nT('apps.issueRadar.components.prBulkBar.auto_merge')}
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
          {NEEDS_BODY.has(pending) && (
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
          {NEEDS_CONFIRM.has(pending) && (
            <>
              <div className="text-[12px] text-danger mb-1.5">
                {i18nT('apps.issueRadar.components.prBulkBar.close_warning', { count })}
              </div>
              <input
                value={confirmText}
                onChange={(e) => setConfirmText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Escape') {
                    e.preventDefault()
                    e.stopPropagation()  // see the comment on the textarea above
                    reset()
                  }
                  if (e.key === 'Enter' && confirmArmed) { e.preventDefault(); apply(pending) }
                }}
                placeholder={BULK_PR_CLOSE_TOKEN}
                aria-label={i18nT('apps.issueRadar.components.prBulkBar.type_to_confirm')}
                className="w-full bg-bg-elevated border border-border rounded-md px-2.5 py-1.5 text-[13px] text-text placeholder:text-muted outline-none transition-colors focus-ring font-body"
              />
            </>
          )}
          <div className="mt-1.5 flex items-center gap-1.5">
            <Btn primary onClick={() => apply(pending)} disabled={!canSubmit || bulk.busy}>
              {bulk.busy
                ? <Loader2 className="lucide-inline animate-spin" />
                : <Check className="lucide-inline" />}
              {i18nT('apps.issueRadar.components.prBulkBar.apply', { count })}
            </Btn>
            <Btn onClick={reset}>{i18nT('apps.issueRadar.components.prBulkBar.cancel')}</Btn>
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
