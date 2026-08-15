// The "Respond" control on a change-request LIST ROW: opens (or resumes) a session
// that answers the feedback that change request received (see lib/respond.ts).
//
// Two deliberate departures from ReviewButton, both because this renders per ROW
// rather than once in a header:
//
// 1. It does NOT subscribe to the record. ReviewButton keeps a `useQuery` per
//    instance so it can render "Review" vs "Resume", which costs one subscription
//    per mounted row — up to 200 on the animated list. This reads the record inside
//    the click instead. That is also strictly more correct for the decision the read
//    drives: a fresh read cannot hand back a 30s-stale "no session exists" and
//    duplicate a session the user already has.
// 2. It is a compact icon button rather than an `AgentSessionButton`, whose
//    hardcoded accent fill is a header-primary look and would dominate the card.
//
// The row group already contains the card's own `<button>`, so this is the second
// and last control the `max-two-buttons-per-row` rule allows there. A further row
// action has to become an overflow menu.
import { useRef, useState } from 'react'
import { MessageSquareReply } from 'lucide-react'
import { issueRadarApi, type DispatchReason, type PullRequest, RepoRef } from '../api'
import { useRespondToPr } from '../lib/respond'
import { providerTerms } from '../lib/links'

import { i18nT } from '../../../i18n/t'

/** What the click-time readiness read refused with.
 *
 * `unverified` is deliberately its own case rather than reusing the cached hint's
 * sentence: a read that FAILED taught us nothing about the checkout, and telling the
 * user to set a repository location they already set sends them to redo the wrong
 * thing. The two real reasons are the server's own, so they stay accurate even when
 * the cached hint has gone stale. */
function refusalText(reason: DispatchReason | 'unverified'): string {
  if (reason === 'unverified') {
    return i18nT('apps.issueRadar.components.prList.respond_verify_failed')
  }
  if (reason === 'checkout_unusable') {
    return i18nT('apps.issueRadar.components.prList.respond_checkout_unusable')
  }
  return i18nT('apps.issueRadar.components.prList.respond_no_local_path')
}

export default function PrRespondButton({
  repoRef, pull, ready, notReadyReason,
}: {
  repoRef: RepoRef
  pull: PullRequest
  /** Cached HINT for whether this repo has a usable local checkout. It decides only
   * whether the control is offered; the path itself is re-read on click. */
  ready: boolean
  /** Why it is not ready, already translated by the owner. */
  notReadyReason: string
}) {
  const terms = providerTerms(repoRef)
  const { respondToPr, busy, error } = useRespondToPr()
  const [lookupFailed, setLookupFailed] = useState(false)
  // Why the authoritative click-time read refused, when the cached hint said ready.
  // The cached reason cannot stand in for this: a transient failure would then tell
  // the user to set a repository location they have already set.
  const [clickRefusal, setClickRefusal] = useState<DispatchReason | 'unverified' | null>(null)
  const [pending, setPending] = useState(false)
  // Synchronous re-entry guard. `busy` cannot do this job: it is owned by
  // `openSession`, which does not run until the record read below resolves, so a
  // second click during that read sees `busy === false` and starts its own
  // session. Both reads then report "no session", both open one, and the later
  // link write overwrites the first -- orphaning a live session, which is the
  // exact outcome reading the record was meant to prevent. React state cannot
  // close the window either, since it is not visible to a click in the same tick.
  const inFlight = useRef(false)
  // A refusal is NOT a blocked state: the reason it shows ends in "try again", and the
  // next click re-reads readiness from the server anyway, so latching the control off
  // would contradict its own instruction and leave the row permanently dead.
  const blocked = busy || pending || !ready

  const onClick = async () => {
    // `blocked` covers what `disabled` used to: the control stays focusable so its
    // reason is reachable, so the refusal has to happen here.
    if (blocked || inFlight.current) return
    inFlight.current = true
    setPending(true)
    setLookupFailed(false)
    setClickRefusal(null)
    try {
      // A FAILED lookup must not be read as "no session exists": acting on that
      // would start a second session and overwrite the existing record's link,
      // orphaning the session the user already has. So a failure reports itself
      // and does nothing, which is recoverable — the user clicks again.
      // Re-read readiness HERE rather than trusting the cached hint. The cache keeps
      // its last successful answer through a failed refetch and exposes no flag for
      // it, so a checkout that broke since then would still read as ready -- and the
      // stale path would be handed to the agent as the directory to work in. The
      // server re-derives readiness on every read, so this is the authoritative one.
      let workingPath: string
      try {
        const readiness = await issueRadarApi.getDispatchReadiness(repoRef)
        if (!readiness.ready || !readiness.local_path) {
          // The server said why; use ITS reason rather than the cached one.
          setClickRefusal(readiness.reason || 'no_local_path')
          return
        }
        workingPath = readiness.local_path
      } catch {
        // Nothing was learned, so say exactly that: naming a setup step the user has
        // already completed sends them to redo work that is not the problem.
        setClickRefusal('unverified')
        return
      }

      let existing = null
      try {
        const res = await issueRadarApi.getInvestigation(repoRef, pull.number, 'pull', 'respond')
        existing = res.investigation
      } catch {
        setLookupFailed(true)
        return
      }
      await respondToPr(repoRef, pull, workingPath, existing)
    } finally {
      inFlight.current = false
      setPending(false)
    }
  }

  const label = i18nT('apps.issueRadar.components.prRespondButton.answer_feedback', {
    subject: terms.changeRequestShort,
    number: pull.number,
  })
  // A failure that just happened outranks every steady-state hint: it is the only thing
  // the user is waiting to hear. Reporting it here at least makes it reachable by
  // hover and by assistive tech -- previously the hook's error was discarded, so a
  // failed open produced no signal anywhere and read as a dead control.
  const title = error
    ? error.message
    : clickRefusal
    ? refusalText(clickRefusal)
    : !ready
      ? notReadyReason
      : lookupFailed
        ? i18nT('apps.issueRadar.components.prRespondButton.lookup_failed')
        : i18nT('apps.issueRadar.components.prRespondButton.answer_feedback_hint', {
            label: terms.changeRequestTitle,
          })

  return (
    <button
      type="button"
      onClick={onClick}
      // aria-disabled rather than `disabled`: a disabled button is removed from the
      // tab order, so a keyboard or screen-reader user could never reach the control
      // to learn the action exists or why it is unavailable. It stays focusable and
      // the handler refuses instead.
      aria-disabled={blocked || undefined}
      aria-label={blocked || error ? `${label} — ${title}` : label}
      title={title}
      className={`mt-3 flex-shrink-0 rounded p-1 transition-colors ${blocked ? 'opacity-40 cursor-not-allowed text-[var(--text-muted)]' : 'text-[var(--text-muted)] hover:text-[var(--accent)] hover:bg-[var(--surface-hover)]'}`}
    >
      <MessageSquareReply size={14} aria-hidden="true" />
    </button>
  )
}
