import { useState } from 'react'
import { Check, ChevronDown, ChevronUp, FolderInput } from 'lucide-react'
import { useQueryClient } from '@tanstack/react-query'
import {
  api,
  ApiError,
  friendlyErrText,
  type ChannelFolderBackfillReport,
} from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'

/** The report `POST /api/channel-folders/backfill` answers with.
 *
 *  Declared on the API client, which owns the wire type now that the request goes
 *  through the shared transport, and re-exported under the name this component has
 *  always published so its readers and tests keep one import. */
export type BackfillReport = ChannelFolderBackfillReport

/** What a failed run says.
 *
 *  An auth-expired denial keeps the TRANSPORT's message. `apiFailure` has already
 *  replaced the gateway's cryptographic reason ("invalid signature") with the
 *  localized sign-in instruction, and the re-auth banner is up beside it, so
 *  putting this panel's own sentence there would hide the one action that
 *  recovers -- the defect this component had while it issued its own `fetch`
 *  (#12127).
 *
 *  Every other rejection keeps its message when the body carried a human one. The
 *  handler answers `{error, code}` on all of its refusals and the transport
 *  unwraps that, so the sentence the user reads is unchanged from before. When the
 *  body carried none -- an edge HTML page, an empty 500 -- this falls back to the
 *  panel's own sentence, which is what the raw-`fetch` version did; without it the
 *  transport's `HTTP 502` would be shown instead. The question is put to
 *  `friendlyErrText`, the same function the transport used to decide, rather than
 *  re-deciding it here from the status. */
function backfillErrorMessage(e: unknown): string {
  const unavailable = i18nT('pages.settings.botChannelPanel.backfill_unavailable')
  if (e instanceof ApiError) {
    if (e.authRequired) return e.message || unavailable
    return friendlyErrText(e.status, e.body) || unavailable
  }
  // A transport-level rejection (the request never reached the gateway) keeps its
  // own message, exactly as it did before.
  return e instanceof Error && e.message ? e.message : unavailable
}

/** Moved sessions named individually, the first {@link NAMED_LIMIT} of them up
 *  front and the rest behind an expander.
 *
 *  The list is the only record of what happened -- there is no bulk undo -- so it
 *  names sessions rather than counting them, and every name stays reachable: a
 *  first run over months of history is exactly the case this feature exists for,
 *  and it is also the case that moves the most. What the cap buys is only that
 *  200 rows do not push the panel's own controls off the screen before the user
 *  has read the result, so it collapses the tail instead of dropping it. */
const NAMED_LIMIT = 8

export function ChannelFolderBackfill(props: {
  /** Channel session-key namespace, e.g. `slack`. */
  namespace: string
  /** Folder name the SERVER currently holds, shown in the copy so the user reads
   *  where conversations will go before clicking. */
  folderName: string
  disabled?: boolean
  testId?: string
}) {
  const { namespace, folderName, disabled, testId } = props
  const qc = useQueryClient()
  const [pending, setPending] = useState(false)
  const [report, setReport] = useState<BackfillReport | null>(null)
  const [error, setError] = useState('')

  const run = () => {
    setPending(true)
    setError('')
    // The previous report is dropped BEFORE the request, not after it returns: a
    // stale "moved 3" sitting under a spinner reads as the current run's result.
    setReport(null)
    void api
      .backfillChannelFolder(namespace)
      .then(answer => {
        setReport(answer)
        // The gateway pushes a slots update for every OPEN tab it re-placed, so
        // the live sidebar moves on its own. These two cover what that push does
        // not: a conversation with no open tab (it only exists in History), and a
        // folder that was hidden and becomes visible now that it holds something.
        void qc.invalidateQueries({ queryKey: ['chat-folders'] })
        // No slots invalidation here, deliberately. `['slots']` and
        // `['chat-slots']` are both DEAD keys: nothing in the dashboard registers
        // a query on either, so `invalidateQueries` traverses an empty match set
        // and returns without a request or an error (#10204, and the ratchet in
        // `chatSlotsDeadKeyInvalidation.test.tsx` exists to stop a seventh such
        // line arriving). A slot row renders from the Redux `dashboard` slice, fed
        // by the websocket `sseSlots` frame -- and this endpoint already ends in
        // `state.push_slots_update()` whenever it touched a live slot, so the
        // refresh this reached for is already on its way.
      })
      .catch((e: unknown) => setError(backfillErrorMessage(e)))
      .finally(() => setPending(false))
  }

  return (
    <div className="mt-4" data-testid={testId}>
      <p className="text-[12.5px] text-muted mt-0 mb-2">
        {i18nT('pages.settings.botChannelPanel.backfill_existing_desc', { folder: folderName })}
      </p>
      <Btn onClick={run} disabled={disabled || pending}>
        <FolderInput size={13} />
        {pending
          ? i18nT('pages.settings.botChannelPanel.backfill_running')
          : i18nT('pages.settings.botChannelPanel.backfill_existing')}
      </Btn>
      {report && <BackfillOutcome report={report} folderName={folderName} />}
      {/* No hand-off: the failure is a refused bulk move, and the panel around
          this button holds the user's unsaved settings draft (the folder name
          they may be mid-edit, and on the token panels a pasted credential).
          An `askAgent` hand-off navigates away from the panel and discards that
          draft, which costs more than the error explains -- and the remedy here
          never needs the agent: the message names it (save the folder name,
          create the folder, or run from the local machine) and the button is
          still there to click again. */}
      {/* In its own block on purpose: `Btn` is inline, so an inline notice
          rendered as its sibling sits BESIDE the button, while every
          report-level notice renders below it. Two failure placements on one
          card makes the reader hunt for which one applies. */}
      <div>
        <ErrorNotice
          variant="inline"
          className="mt-2 text-[11.5px]"
          message={error}
          testId={testId ? `${testId}-error` : undefined}
        />
      </div>
    </div>
  )
}

/** What one completed run says. Split out so each outcome is one branch rather
 *  than a chain of ternaries inside the button's JSX. */
function BackfillOutcome(props: { report: BackfillReport; folderName: string }) {
  const { report, folderName } = props
  // Collapsed for each new report rather than remembered: the button drops the
  // previous report before it requests, so this component unmounts between runs
  // and a later run's list cannot inherit an earlier one's expanded state.
  const [expanded, setExpanded] = useState(false)
  // The folder the SERVER acted on wins over the panel's copy of the name: on a
  // `folder_missing` answer they are the same, but after a rename that has not
  // been saved yet they differ, and the honest thing to name is where the
  // conversations actually went.
  const folder = report.folder_name || folderName

  // The reason and the receipt render TOGETHER rather than one INSTEAD of the
  // other. A folder deleted mid-pass sets `folder_missing` AFTER conversations
  // have already been filed -- the path
  // `test_a_folder_deleted_mid_pass_strands_nothing` exercises exactly that -- and
  // the named list is the only record of what moved, since there is no bulk undo.
  // Returning the note alone discarded precisely the evidence the user needs to
  // put those conversations back by hand.
  const reasonNote = (() => {
    if (report.reason === 'not_configured') {
      return <BackfillNote text={i18nT('pages.settings.botChannelPanel.backfill_not_configured')} />
    }
    if (report.reason === 'folder_missing') {
      // No folder answered to that name when the pass STARTED, so nothing was
      // attempted and saving the settings creates it. That is the whole remedy.
      return <BackfillNote text={i18nT('pages.settings.botChannelPanel.backfill_folder_missing', { folder })} />
    }
    if (report.reason === 'folder_gone') {
      // A DIFFERENT value from the one above, because the server distinguishes them
      // now. It used to report both as `folder_missing`, which left this component
      // proving "the folder existed" from `failed > 0` -- an inference about the
      // server's control flow, made a layer away from it.
      //
      // `moved` still picks the sentence, and that is not the same kind of claim:
      // whether a receipt exists is a fact this report carries about itself. With
      // one, the already-filed sessions are stranded on a dead folder id, because
      // recreating the folder mints a fresh one. With none, nothing was stamped,
      // so saving the settings and clicking again really does work.
      return (
        <BackfillNote
          text={
            report.moved.length > 0
              ? i18nT('pages.settings.botChannelPanel.backfill_folder_gone', { folder })
              : i18nT(
                  'pages.settings.botChannelPanel.backfill_folder_gone_nothing_filed',
                  { folder },
                )
          }
        />
      )
    }
    if (report.reason === 'all_failed') {
      // Every write failed and nothing moved. The failure notice below owns this
      // case, because it is the one carrying the COUNT. Falling through to the
      // branch below would state the wrong cause: its message says the session
      // history could not be read, and every read here succeeded.
      return null
    }
    if (report.reason) {
      // A FAILURE, not guidance: the server reaches this when the conversation
      // store is absent or its listing raised. The two
      // branches above are 200s that tell the user what to do next, so they stay
      // plain notes; this one is an error and belongs on the error surface.
      //
      // No hand-off: the panel around this button holds the user's unsaved
      // settings draft (the folder name they may be mid-edit, and on the token
      // panels a pasted credential), and an `askAgent` hand-off navigates away and
      // discards it. The remedy never needs the agent either -- the store was
      // unreadable, and the button is still there to click again.
      return (
        <ErrorNotice
          variant="inline"
          className="mt-2 text-[11.5px]"
          message={i18nT('pages.settings.botChannelPanel.backfill_unavailable')}
          testId="backfill-store-failure"
        />
      )
    }
    if (report.moved.length === 0) {
      return <BackfillNote text={i18nT('pages.settings.botChannelPanel.backfill_none')} />
    }
    return null
  })()

  // Hoisted because failures do not require a success. Two measured shapes
  // report `failed > 0` with an empty `moved`: every write failing, and a write
  // failing before the folder vanished mid-pass. Returning the reason note alone
  // dropped the count in exactly those cases -- the count that tells the user
  // whether clicking again can help.
  const failureNotice =
    report.failed > 0 ? (
      <ErrorNotice
        variant="inline"
        className="mt-2 text-[11.5px]"
        message={
          report.failed >= report.remaining
            ? i18nT('pages.settings.botChannelPanel.backfill_failed_all', {
                count: report.failed,
              })
            : i18nT('pages.settings.botChannelPanel.backfill_remaining_failed', {
                count: report.remaining,
                failed: report.failed,
              })
        }
        testId="backfill-write-failures"
      />
    ) : null

  if (report.moved.length === 0) {
    if (!reasonNote && !failureNotice) return null
    // A BLOCK, not a fragment. A fragment lets a lone notice lay out on the
    // button's own row, so a failure with nothing moved sat BESIDE the button
    // while the identical failure with a receipt sat below it -- and one of these
    // cards only looked right because its note happens to be a block. Every
    // notice belongs in the same place, which is what the UX read asked for.
    //
    // No `role` here: each child carries its own, and there is no receipt for a
    // `status` region to describe.
    return (
      <div data-testid="backfill-notices">
        {reasonNote}
        {failureNotice}
      </div>
    )
  }

  const named = expanded ? report.moved : report.moved.slice(0, NAMED_LIMIT)
  return (
    // No `role` on this wrapper. It holds the receipt AND, when a write failed,
    // an `ErrorNotice` carrying `role="alert"`; an assertive alert nested inside a
    // polite `status` region is announced as part of that region instead of
    // interrupting, which is the failure the `errors-use-error-notice` rule
    // exists to prevent. Status semantics belong on the non-error output only:
    // the receipt sentence below, and `BackfillNote`, which carries its own.
    <div className="mt-2" data-testid="backfill-result">
      {reasonNote}
      <p
        className="inline-flex items-center gap-1.5 text-[12px] text-ok mt-0 mb-1"
        role="status"
      >
        <Check size={13} />
        {i18nT('pages.settings.botChannelPanel.backfill_moved', {
          count: report.moved.length,
          folder,
        })}
      </p>
      <ul className="list-none pl-0 m-0 text-[11.5px] text-muted">
        {named.map(m => (
          <li key={m.key} className="truncate">
            {m.title || i18nT('pages.settings.botChannelPanel.backfill_untitled', { channel: m.label })}
          </li>
        ))}
      </ul>
      {report.moved.length > NAMED_LIMIT && (
        <button
          type="button"
          onClick={() => setExpanded(v => !v)}
          aria-expanded={expanded}
          data-testid="backfill-expand"
          className="mt-1 inline-flex items-center gap-1 text-[11.5px] text-accent underline decoration-dotted underline-offset-2 hover:decoration-solid cursor-pointer bg-transparent border-none p-0 text-left"
        >
          {expanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}
          {expanded
            ? i18nT('pages.settings.botChannelPanel.backfill_show_fewer')
            : i18nT('pages.settings.botChannelPanel.backfill_show_all', {
                count: report.moved.length,
              })}
        </button>
      )}
      {/* A capped run failed at nothing, so it stays a plain note: it is
          guidance that another click continues, not a failure report. */}
      {report.remaining > 0 && report.failed === 0 && (
        <BackfillNote
          text={i18nT('pages.settings.botChannelPanel.backfill_remaining', {
            count: report.remaining,
          })}
        />
      )}
      {/* A write FAILED, so this belongs on the error surface and not in a muted
          status note: rendering a failure as a status is the silent-failure class
          the error-surface rule exists to prevent, and it is the same mistake
          already corrected once on this component's `unavailable` branch.

          When every outstanding conversation is a failure, say that plainly
          rather than making the reader subtract one count from another to work
          out whether retrying can help.

          No hand-off: the panel around this button holds the user's unsaved
          settings draft (the folder name they may be mid-edit, and on the token
          panels a pasted credential), and an `askAgent` hand-off navigates away
          and discards it. The remedy never needs the agent either -- the writes
          failed, and the button is still there to click again. */}
      {failureNotice}
    </div>
  )
}

function BackfillNote(props: { text: string }) {
  return (
    <p className="text-[11.5px] text-muted mt-2 mb-0" role="status">
      {props.text}
    </p>
  )
}
