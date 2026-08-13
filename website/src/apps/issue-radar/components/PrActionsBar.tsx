// The per-PR actions bar in the pull-request detail header.
//
// The actions a maintainer otherwise leaves the app to perform on the provider's
// web UI: approve / request changes, comment, close or reopen, merge, and arm the
// provider's own auto-merge.
//
// Three deliberate shapes:
//
//  * **Two merge affordances, neither of which can bypass a gate.** "Merge" lands a
//    PR the provider already reports as mergeable; "Auto-merge" hands it to the
//    provider to land once its required reviews and checks pass. Branch protection is
//    enforced on the provider's own endpoints, so the button cannot merge something
//    unreviewed — and offering only auto-merge would leave a repo with no branch
//    rule, where auto-merge is unavailable, with no merge path at all.
//  * **Verdict actions that need prose open a composer**, they do not fire on
//    click. "Request changes" without a reason is rejected by the provider anyway,
//    and an approval is worth a sentence.
//  * **Actions the provider will refuse are not offered.** A merged PR cannot be
//    reopened and cannot be armed, a closed PR cannot be approved — the bar reads
//    the PR's real lifecycle and renders only what applies, rather than showing a
//    button that returns an error.
import { useEffect, useRef, useState } from 'react'
import {
  Check, CircleSlash, CircleDot, MessageSquarePlus, GitMerge, X, Loader2, AlertTriangle,
} from 'lucide-react'
import { Btn } from '../../../components/ui'
import { usePrActions, PR_ACTION, isMergeReady, canArmAutoMerge as rowCanArm } from '../lib/prActions'
import { providerTerms, isGitlab } from '../lib/links'
import type { PrDetailData, PullRequest, RepoRef } from '../api'

import { i18nT } from '../../../i18n/t'

/** Which composer is open, if any. `null` means the bar is showing its buttons. */
type Composer = 'approve' | 'request_changes' | 'comment' | null

/** Whether the provider rejects this verb without a body.
 *
 * Data, not copy, so it lives at module scope: the provider accepts a bodyless
 * APPROVE but rejects a bodyless REQUEST_CHANGES or COMMENT. The button stays
 * disabled for the latter two — a client-side guard for a server-side rule.
 */
const REQUIRES_BODY: Record<Exclude<Composer, null>, boolean> = {
  approve: false,
  request_changes: true,
  comment: true,
}

/**
 * The composer's copy, resolved PER RENDER rather than at module load.
 *
 * A module-scope `i18nT()` runs once at import, before the user has picked a
 * language, and the string it returns is then frozen for the life of the tab — so
 * this panel would stay English while everything around it translated
 * (`src/i18n/moduleLevel.test.ts` pins the rule).
 */
function composerCopy(kind: Exclude<Composer, null>): {
  title: string; placeholder: string; submit: string; requiresBody: boolean
} {
  const copy = {
    approve: {
      title: i18nT('apps.issueRadar.components.prActionsBar.approve_title'),
      placeholder: i18nT('apps.issueRadar.components.prActionsBar.approve_placeholder'),
      submit: i18nT('apps.issueRadar.components.prActionsBar.approve_submit'),
    },
    request_changes: {
      title: i18nT('apps.issueRadar.components.prActionsBar.request_changes_title'),
      placeholder: i18nT('apps.issueRadar.components.prActionsBar.request_changes_placeholder'),
      submit: i18nT('apps.issueRadar.components.prActionsBar.request_changes_submit'),
    },
    comment: {
      title: i18nT('apps.issueRadar.components.prActionsBar.comment_title'),
      placeholder: i18nT('apps.issueRadar.components.prActionsBar.comment_placeholder'),
      submit: i18nT('apps.issueRadar.components.prActionsBar.comment_submit'),
    },
  }[kind]
  return { ...copy, requiresBody: REQUIRES_BODY[kind] }
}

export default function PrActionsBar({
  repoRef, pull, detail, canWrite,
}: {
  repoRef: RepoRef
  pull: PullRequest
  detail?: PrDetailData
  /** False for a repo connected read-only — every action would 403, so the bar
   * says why instead of offering buttons that fail. */
  canWrite: boolean
}) {
  const terms = providerTerms(repoRef)
  const actions = usePrActions(repoRef, pull.number)
  const [composer, setComposer] = useState<Composer>(null)
  const [text, setText] = useState('')
  const textRef = useRef<HTMLTextAreaElement>(null)

  // Prefer the live detail over the list row: the row can be minutes old, and
  // acting on a stale lifecycle is how "reopen" appears on a PR that was merged
  // meanwhile.
  const merged = detail?.merged ?? Boolean(pull.merged_at)
  const closed = (detail?.state ?? pull.state) === 'closed'
  const autoMergeOn = Boolean(detail?.auto_merge)
  // The commit a review is a verdict ON. Read from the live detail only — never the
  // list row, which can be minutes old: the point of the pin is that the sha is the one
  // this pane actually rendered. Empty until the detail lands, which is what gates the
  // two verdict buttons below.
  const liveSha = detail?.head_sha ?? ''
  // …and the composer submits the sha that was showing WHEN IT OPENED, not the latest
  // polled one.
  //
  // The detail query polls, so reading the live value at submit time meant a force-push
  // landing while the reviewer typed silently re-pointed the approval at the new head —
  // the exact defect the server-side pin exists to prevent, reintroduced on the client
  // where the pin cannot catch it, because the request would carry the NEW sha and the
  // server would have nothing to refuse. Freezing it at open turns that race into the
  // provider's own 422 instead of a recorded verdict on code nobody read.
  const composerSha = useRef('')
  const reviewSha = composer ? composerSha.current : liveSha
  // GitLab has no arm verb this app can drive safely — the flag rides on the merge
  // endpoint and merges immediately with no pipeline running, so the client refuses
  // it (see gitlab_client.enable_auto_merge). Offering the control there would only
  // ever produce an error, which is the "do not offer what the provider will refuse"
  // rule this bar follows everywhere else. An MR armed on GitLab's own UI still
  // DISPLAYS as armed via the read-side detail field.
  const providerArms = !isGitlab(repoRef)
  // Offer ARMING only for a PR the provider will actually accept it on. GitHub
  // refuses `enablePullRequestAutoMerge` for a PR that is already mergeable ("clean
  // status"), already merged, a draft, or `dirty` (a conflict), and answers a cold
  // read with `unknown` — so `canArmAutoMerge` excludes all of those (see
  // lib/prActions and the spec's four-refusals rule). Read from the live detail, not
  // the stale list row. Without this gate the button was offered on every open PR and
  // failed with a provider error on the reflexive case: a ready PR the user wanted to
  // merge. This is the per-PR twin of the readiness gate the bulk bar already applies.
  const armable = providerArms && rowCanArm({
    state: detail?.state ?? pull.state,
    draft: detail?.draft ?? pull.draft,
    merged_at: detail?.merged_at ?? pull.merged_at,
    mergeable_state: detail?.mergeable_state,
  })
  // The single button is BOTH affordances: it arms when off and cancels when on.
  // Show it when arming is meaningful, OR when auto-merge is already armed so the
  // cancel path stays reachable even once the PR has become mergeable (readiness no
  // longer `armable`) — hiding it then would strand an armed PR with no way to disarm.
  const showAutoMerge = providerArms && (armable || autoMergeOn)
  // `mergeable` is the provider's own verdict, and it is TRI-state: null means it is
  // still computing, so the button waits rather than flashing in and out. A draft is
  // never mergeable regardless of what the field says.
  // Gated on the provider's MERGE STATE, not on `mergeable`.
  //
  // `mergeable` means only "no merge conflicts": a PR whose required reviews or checks
  // have not passed is `mergeable: true` with `mergeable_state: "blocked"`. The
  // provider 405s that for an ordinary user, but honours it for an admin holding
  // bypass-branch-protection — so gating on `mergeable` offered exactly the most
  // privileged account a one-click way to land a PR its own rules had rejected. The
  // server enforces the same set (`_MERGE_ALLOWED_STATES`); this keeps the button from
  // appearing when it would only be refused.
  //
  // The head sha is required too: the merge is PINNED to the commit this pane
  // rendered, so a push landing in between is refused rather than merged.
  const mergeable = isMergeReady(detail?.mergeable_state)
    && detail?.mergeable === true
    && !(detail?.draft ?? pull.draft ?? false)
    && Boolean(detail?.head_sha)
  // A merged PR is finished: nothing here applies to it. A CLOSED-unmerged one can
  // still be reopened, which is the one action it keeps.
  const terminal = merged

  useEffect(() => {
    if (composer) textRef.current?.focus()
  }, [composer])

  // Reset when the pane switches to another PR, so a half-typed review cannot be
  // submitted against the wrong one — and so a failure from the PREVIOUS PR does
  // not sit on this one. The error lives in the hook, and the pane is not remounted
  // per PR, so without clearing it a maintainer reads "Action failed · PR #7 is
  // locked" while looking at #8.
  useEffect(() => {
    setComposer(null)
    setText('')
    actions.clearError()
    // `actions.clearError` is a stable useCallback; depending on it would be
    // harmless but re-running this on an unrelated identity change is not the
    // intent — the trigger is the PR switching.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pull.number])

  /** Open a composer, freezing the head commit it will submit against.
   *
   * The ONE place a composer opens, so the snapshot cannot be forgotten at one of the
   * three call sites — which is how the live-sha read got there in the first place.
   */
  const openComposer = (kind: Exclude<Composer, null>) => {
    composerSha.current = liveSha
    setComposer(kind)
  }

  const closeComposer = () => {
    setComposer(null)
    setText('')
    actions.clearError()
  }

  const submitComposer = async () => {
    // `actions.busy` too: Cmd/Ctrl+Enter can fire again while the first request is
    // still in flight, which posted a duplicate comment or review.
    if (!composer || actions.busy) return
    const body = text.trim()
    if (REQUIRES_BODY[composer] && !body) return
    // A REVIEW is a verdict on a revision, so it cannot be submitted until this pane
    // knows which commit it is looking at (the detail read is what supplies it). A
    // plain comment is not a verdict and needs no pin.
    if (composer !== 'comment' && !reviewSha) return
    const result = composer === 'approve'
      ? await actions.approve(reviewSha, body || undefined)
      : composer === 'request_changes'
        ? await actions.requestChanges(reviewSha, body)
        : await actions.comment(body)
    // Only clear on success — a failed submit keeps the text so the user does not
    // retype a paragraph after a transient error.
    if (result) closeComposer()
  }

  if (!canWrite) {
    return (
      <div className="flex items-center gap-1.5 text-[12px] text-muted">
        <CircleSlash className="lucide-inline" />
        {i18nT('apps.issueRadar.components.prActionsBar.read_only_repo')}
      </div>
    )
  }

  if (terminal) {
    return (
      <div className="flex items-center gap-1.5 text-[12px] text-muted">
        <GitMerge className="lucide-inline" />
        {i18nT('apps.issueRadar.components.prActionsBar.already_merged')}
      </div>
    )
  }

  if (composer) {
    const copy = composerCopy(composer)
    const blocked = copy.requiresBody && !text.trim()
    return (
      <div className="w-full min-w-0">
        <div className="flex items-center justify-between gap-2 mb-1.5">
          <span className="text-[12px] font-medium text-text-strong">{copy.title}</span>
          <Btn
            onClick={closeComposer}
            aria-label={i18nT('apps.issueRadar.components.prActionsBar.cancel')}
            className="px-1.5 py-0.5"
          >
            <X className="lucide-inline" />
          </Btn>
        </div>
        <textarea
          ref={textRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape') { e.preventDefault(); closeComposer() }
            // Cmd/Ctrl+Enter submits, matching the chat composer. A bare Enter
            // stays a newline: these bodies are prose, often multi-line.
            if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); submitComposer() }
          }}
          placeholder={copy.placeholder}
          aria-label={copy.title}
          rows={3}
          className="w-full bg-bg-elevated border border-border rounded-md px-2.5 py-2 text-[13px] text-text placeholder:text-muted outline-none resize-y transition-colors focus-ring font-body"
        />
        {actions.error && (
          <div className="mt-1.5 flex items-start gap-1.5 text-[12px] text-danger">
            <AlertTriangle className="lucide-inline flex-shrink-0" />
            <span className="min-w-0 break-words">{actions.error.message}</span>
          </div>
        )}
        <div className="mt-1.5 flex items-center gap-1.5">
          <Btn
            primary
            onClick={submitComposer}
            disabled={blocked || Boolean(actions.busy)}
            title={blocked
              ? i18nT('apps.issueRadar.components.prActionsBar.needs_a_reason')
              : copy.submit}
          >
            {actions.busy
              ? <Loader2 className="lucide-inline animate-spin" />
              : <Check className="lucide-inline" />}
            {copy.submit}
          </Btn>
          <span className="text-[11px] text-muted">
            {i18nT('apps.issueRadar.components.prActionsBar.submit_hint')}
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      {actions.error && (
        <Btn
          danger
          onClick={actions.clearError}
          title={actions.error.message}
          aria-label={i18nT('apps.issueRadar.components.prActionsBar.dismiss_error')}
        >
          <AlertTriangle className="lucide-inline" />
          {i18nT('apps.issueRadar.components.prActionsBar.action_failed')}
        </Btn>
      )}

      {/* A closed PR keeps only "reopen": approving or arming auto-merge on it
          would be refused by the provider. The two VERDICT buttons additionally wait
          for the head commit — a review names the revision it was formed on, so
          until the detail read lands there is nothing to pin one to. */}
      {!closed && Boolean(reviewSha) && (
        <>
          <Btn
            onClick={() => openComposer('approve')}
            disabled={Boolean(actions.busy)}
            // The provider's own word for the item ("pull request" / "merge
            // request") is interpolated, so a GitLab workspace never reads "PR".
            title={i18nT('apps.issueRadar.components.prActionsBar.approve_hint', {
              subject: terms.changeRequestTitle,
            })}
          >
            <Check className="lucide-inline" />
            {i18nT('apps.issueRadar.components.prActionsBar.approve')}
          </Btn>
          <Btn
            onClick={() => openComposer(PR_ACTION.requestChanges)}
            disabled={Boolean(actions.busy)}
            title={i18nT('apps.issueRadar.components.prActionsBar.request_changes_hint', {
              subject: terms.changeRequestTitle,
            })}
          >
            <CircleSlash className="lucide-inline" />
            {i18nT('apps.issueRadar.components.prActionsBar.request_changes')}
          </Btn>
        </>
      )}

      <Btn
        onClick={() => openComposer('comment')}
        disabled={Boolean(actions.busy)}
        title={i18nT('apps.issueRadar.components.prActionsBar.comment_hint', {
          subject: terms.changeRequestTitle,
        })}
      >
        <MessageSquarePlus className="lucide-inline" />
        {i18nT('apps.issueRadar.components.prActionsBar.comment')}
      </Btn>

      {/* MERGE, offered only when the provider says the PR is mergeable right now.
          It cannot bypass a gate — branch protection is enforced on the provider's
          own endpoint — but showing it on a blocked PR would present a button whose
          only outcome is a refusal, which is the "do not offer what the provider
          will refuse" rule this bar follows everywhere else. Auto-merge below is the
          affordance for exactly that case. */}
      {!closed && mergeable && (
        <Btn
          primary
          // The sha THIS RENDER showed, closed over by the handler. Merge has no
          // typing window like the composer, so one render tick is the whole
          // exposure — and the server re-reads the PR and refuses a moved head
          // (409 `merge_conflict`) regardless.
          onClick={() => actions.merge(liveSha)}
          disabled={Boolean(actions.busy)}
          title={i18nT('apps.issueRadar.components.prActionsBar.merge_hint', {
            subject: terms.changeRequestTitle,
          })}
        >
          {actions.busy === PR_ACTION.merge
            ? <Loader2 className="lucide-inline animate-spin" />
            : <GitMerge className="lucide-inline" />}
          {i18nT('apps.issueRadar.components.prActionsBar.merge')}
        </Btn>
      )}

      {!closed && showAutoMerge && (
        <Btn
          onClick={() => actions.setAutoMerge(!autoMergeOn)}
          disabled={Boolean(actions.busy)}
          // The label states what will HAPPEN, and the tooltip states what
          // auto-merge is — the distinction between this and "merge now" is the
          // whole point, so it is said in the UI rather than only in the code.
          title={autoMergeOn
            ? i18nT('apps.issueRadar.components.prActionsBar.auto_merge_off_hint')
            : i18nT('apps.issueRadar.components.prActionsBar.auto_merge_on_hint')}
          className={autoMergeOn ? 'text-aim border-aim' : undefined}
        >
          {actions.busy === PR_ACTION.autoMerge || actions.busy === PR_ACTION.cancelAutoMerge
            ? <Loader2 className="lucide-inline animate-spin" />
            : <GitMerge className="lucide-inline" />}
          {autoMergeOn
            ? i18nT('apps.issueRadar.components.prActionsBar.auto_merge_cancel')
            : i18nT('apps.issueRadar.components.prActionsBar.auto_merge_enable')}
        </Btn>
      )}

      {closed ? (
        <Btn
          onClick={actions.reopen}
          disabled={Boolean(actions.busy)}
          title={i18nT('apps.issueRadar.components.prActionsBar.reopen_hint', {
            subject: terms.changeRequestTitle,
          })}
        >
          {actions.busy === PR_ACTION.reopen
            ? <Loader2 className="lucide-inline animate-spin" />
            : <CircleDot className="lucide-inline" />}
          {i18nT('apps.issueRadar.components.prActionsBar.reopen')}
        </Btn>
      ) : (
        <Btn
          danger
          onClick={actions.close}
          disabled={Boolean(actions.busy)}
          title={i18nT('apps.issueRadar.components.prActionsBar.close_hint', {
            subject: terms.changeRequestTitle,
          })}
        >
          {actions.busy === PR_ACTION.close
            ? <Loader2 className="lucide-inline animate-spin" />
            : <CircleSlash className="lucide-inline" />}
          {i18nT('apps.issueRadar.components.prActionsBar.close')}
        </Btn>
      )}
    </div>
  )
}
