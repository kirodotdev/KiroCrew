/** Resume every interrupted idle session at once: the count the row shows, the
 *  one write (`useResumeAll`), and the shelf row that drives it
 *  (`renderResumeAllRow`).
 *
 *  A gateway restart or a run of backend errors leaves many sessions with a turn
 *  that ended without a reply -- the state behind the composer's Resume button
 *  and the sidebar row's "interrupted" cue. Pressing Resume in each tab is the
 *  chore this row removes: one press calls `POST /api/chat/slots/resume-all`,
 *  which answers at once with the run's id and runs the Continue path on every
 *  such session in the background, a few at a time: the next session starts as
 *  an earlier one's turn ends. Above `RESUME_ALL_CONFIRM_ABOVE` sessions the
 *  press asks first, since every one of them starts a turn. The row stays
 *  pending until the run's outcome arrives as a `resume_all_done` frame (the
 *  rows themselves move with the ordinary slots push meanwhile, and a session
 *  the run has not reached yet says it is waiting, `resumeAllWaiting`). The frame can
 *  arrive before the 202 has been read -- a short run finishes in milliseconds
 *  -- so the hook keeps each outcome it hears while its request is open, by run
 *  id, and settles from the one the 202 names. A press while a run is in
 *  flight -- another tab's -- gets 409 `resume_all_running` with that run's id
 *  and total; the row then follows that run and says it is already going.
 *
 *  It is a row in the search dock's SHELF, not a third control in the search
 *  row's trailing cluster: AUTOSDE max-two-buttons-per-row grandfathers that
 *  cluster but forbids growing it (the "Pause all filters" row went into the
 *  menu for the same reason). The shelf already carries the list-level notices
 *  in the same `role="status"` grammar (`folderReorderHint`), so the row reads
 *  as one of them: a sentence with the count, and the one action beside it. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import { api } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { useConfirm } from '../../components/ConfirmDialog'
import { MORE_BELOW_MASK } from '../../components/ModelEffortDropdown'
import { Btn } from '../../components/ui'
import { useScrollEdgesY } from '../../hooks/useScrollEdges'
import { crewModeEnabled } from '../../lib/crewMode'
import { RESUME_ALL_DONE_EVENT } from '../../lib/resumeAllEvent'
import { setResumeAllWaiting } from '../../lib/resumeAllWaiting'
import { i18nT } from '../../i18n/t'
import { offlineProps } from '../../utils/offline'
import type { Slot } from './types'
import type { SidebarScope } from './scope'

/** How long the outcome line stays after a run before the row returns to
 *  its count (or disappears, once the slots push has cleared the badge). */
export const RESUME_ALL_RESULT_MS = 6000

/** Above this many sessions a press asks for confirmation first: each one
 *  starts an agent turn and spends credits, and a few is what a restart
 *  usually leaves. */
export const RESUME_ALL_CONFIRM_ABOVE = 5

/** How many resumed turns the server runs at once, the number the
 *  confirmation names. The server's `RESUME_ALL_MAX_RUNNING`
 *  (`dashboard/chat_resume_all.py`); `test_chat_resume_all.py` holds the two
 *  equal. */
export const RESUME_ALL_MAX_RUNNING = 4

/** The backstop on a run's wait, per session. The run starts a few sessions at
 *  a time and the next one only as an earlier turn ends, so a long run is
 *  normal; a lost frame is not, because frames only go missing while the
 *  socket is down, and the row stops waiting the moment it goes down. */
export const RESUME_ALL_PENDING_PER_SESSION_MS = 5 * 60_000

/** How long the row waits for a run's outcome frame before giving the button
 *  back, whatever else happens. A press after that, with the run still going,
 *  gets the 409 and follows the run again. */
export function resumeAllPendingCeilingMs(total: number): number {
  return 60_000 + Math.max(0, total) * RESUME_ALL_PENDING_PER_SESSION_MS
}

/** The 409 a press gets while another press's run is still going. */
export const RESUME_ALL_RUNNING_CODE = 'resume_all_running'

/** The sessions the server will act on, read from the same slots payload the
 *  rows render: idle AND flagged interrupted by the transcript scan. Peer rows
 *  (`peer_id`) belong to another gateway and never count -- the request goes to
 *  this one. A row still reporting live sub-agents is left out here and by the
 *  server's candidate set alike, so the count on the button is the `total` the
 *  server answers. */
export function interruptedIdleSlots(slots: Slot[]): Slot[] {
  return slots.filter(s => !!s.interrupted && !s.running && !s.subagents_running && !s.peer_id && s.executor !== 'remote')
}

export type ResumeAllResult = {
  ok: boolean
  total: number
  run_id?: string
  resumed: string[]
  skipped: { slot: string; code: string }[]
  failed: { slot: string; error: string }[]
  cancelled?: boolean
}

/** Skip codes that need nothing from anyone: the session moved on between the
 *  count and its dispatch (it finished, started, queued a message this process
 *  will drain on its own, began stopping, closed, or started sub-agents). A queue
 *  a restart restored from disk is no skip: the run resumes that session. Every other skip, and every failure, is one
 *  the owner must act on, so the row reports it through `ErrorNotice`. */
export const RESUME_ALL_BENIGN_SKIPS: ReadonlySet<string> = new Set([
  'turn_not_failed', 'slot_running', 'slot_queue_pending', 'slot_stopping',
  'slot_not_found', 'slot_subagents_running',
])

/** One session a run did not resume for a reason the owner must act on. */
export type ResumeAllProblem = { slot: string; reason: 'start_repeat' | 'approval' | 'error' | 'other' }

/** The sessions in *res* that need the owner: every failure, and every skip
 *  outside `RESUME_ALL_BENIGN_SKIPS`, in the order the server listed them. */
export function resumeAllProblems(res: ResumeAllResult): ResumeAllProblem[] {
  const out: ResumeAllProblem[] = []
  for (const { slot, code } of res.skipped) {
    if (RESUME_ALL_BENIGN_SKIPS.has(code)) continue
    out.push({
      slot,
      reason: code === 'session_start_repeat' ? 'start_repeat' : code === 'slot_approval_pending' ? 'approval' : 'other',
    })
  }
  for (const { slot } of res.failed) out.push({ slot, reason: 'error' })
  return out
}

/** The problems list's own box: exactly six lines (`leading-4` sets 16px lines,
 *  `max-h-24` is 96px), scrolling past that, so a long list never pushes the
 *  notice's controls off the panel and the box's edge falls between two lines,
 *  never through one. */
export const RESUME_ALL_PROBLEMS_MESSAGE_CLASS = 'block whitespace-pre-line leading-4 max-h-24 overflow-y-auto'

/** One line per problem: the session's title, then what stopped it. */
export function resumeAllProblemText(problems: ResumeAllProblem[], titleOf: (key: string) => string): string {
  const reasonText = (reason: ResumeAllProblem['reason']): string => {
    switch (reason) {
      case 'start_repeat': return i18nT('pages.chatSidebar.resume_all_reason_start_repeat')
      case 'approval': return i18nT('pages.chatSidebar.resume_all_reason_approval')
      case 'error': return i18nT('pages.chatSidebar.resume_all_reason_error')
      default: return i18nT('pages.chatSidebar.resume_all_reason_other')
    }
  }
  return problems.map(p => `${titleOf(p.slot)}: ${reasonText(p.reason)}`).join('\n')
}

type Refusal = { status: number; code: string; runId: string; total: number }

/** Status, JSON `code`, `run_id` and `total` of a failed request, each empty
 *  (or 0) when absent. */
function refusalOf(e: unknown): Refusal {
  const r = e as { status?: unknown; body?: unknown }
  const status = typeof r?.status === 'number' ? r.status : 0
  const none: Refusal = { status, code: '', runId: '', total: 0 }
  if (typeof r?.body !== 'string') return none
  try {
    const b = JSON.parse(r.body) as { code?: unknown; run_id?: unknown; total?: unknown }
    return {
      status,
      code: typeof b.code === 'string' ? b.code : '',
      runId: typeof b.run_id === 'string' ? b.run_id : '',
      total: typeof b.total === 'number' && b.total > 0 ? b.total : 0,
    }
  } catch { return none }
}

/** The owner gate refusing the viewer: this row's route is owner-only, and a
 *  non-owner dashboard session cannot fix that by retrying, so its notice
 *  names the owner gate instead of the raw refusal. */
export function isOwnerOnlyRefusal(e: unknown): boolean {
  const { status, code } = refusalOf(e)
  return status === 403 && code === 'owner_only'
}

/** What the failure notice says under its title. Never the server's own text
 *  ("Internal Server Error" tells the owner nothing): the owner gate gets its
 *  own sentence, and every other failure says nothing was resumed and what to
 *  do next. */
export function resumeAllErrorText(e: unknown): string {
  return isOwnerOnlyRefusal(e)
    ? i18nT('pages.chatSidebar.resume_all_owner_only')
    : i18nT('pages.chatSidebar.resume_all_failed_next')
}

type WaitedRun = { id: string; total: number; adopted: boolean }

/** The row's state: the candidate count, the write, and the run it waits on. */
export function useResumeAll({ localSlots, connected }: { localSlots: Slot[]; connected: boolean }) {
  const candidates = useMemo(() => interruptedIdleSlots(localSlots), [localSlots])
  const count = candidates.length
  const { confirm, confirmDialog } = useConfirm()
  // The sessions counted at the press that the run has not reached yet: the
  // rows that say they are waiting while the run is pending. A session leaves
  // the list for good once a slots push shows it running or no longer
  // interrupted, so one whose resumed turn failed again reads Resume again.
  const pressedKeysRef = useRef<string[]>([])
  // The connection after the confirmation, which can stay open across a
  // dropped socket.
  const connectedRef = useRef(connected)
  connectedRef.current = connected
  // The outcome of the LAST run, shown for a few seconds in place of the
  // count. Kept as data, not prose, so the row renders it in the active
  // language even if the language switches while it is up.
  const [result, setResult] = useState<ResumeAllResult | null>(null)
  const [error, setError] = useState('')
  // The sessions the last run left for the owner to act on. Kept until
  // dismissed or the next press, not for the outcome line's six seconds.
  const [problems, setProblems] = useState<ResumeAllProblem[]>([])
  const settle = (res: ResumeAllResult) => {
    setResult(res)
    setProblems(resumeAllProblems(res))
  }
  // The run this row waits on: its id, its candidate count, and whether it is
  // another press's run that this press found already going (the 409).
  // The ref is written where the 202/409 body is read, not from render: the
  // outcome frame is a separate macrotask and must see the id at once.
  const [waited, setWaited] = useState<WaitedRun | null>(null)
  const waitedRef = useRef<WaitedRun | null>(null)
  // The outcome frames heard while this row's request is open, by run id. The
  // 202 or 409 names its run, and that run can finish before the answer is
  // read. More than one run can: the server's one-run guard ends when a run's
  // dispatch is done, not when its 202 reaches this tab, so another tab's run
  // can start and finish in between. Emptied when the answer is read, so no
  // frame carries over to a later press.
  const earlyRef = useRef<Map<string, ResumeAllResult>>(new Map())
  const requestOpenRef = useRef(false)
  const setWait = (next: WaitedRun | null) => {
    waitedRef.current = next
    setWaited(next)
  }
  // Wait on *run*, or settle at once if its outcome frame already arrived.
  const follow = (run: WaitedRun) => {
    const early = run.id ? earlyRef.current.get(run.id) : undefined
    earlyRef.current.clear()
    if (early) {
      setWait(null)
      settle(early)
      return
    }
    setWait(run)
  }
  const mutation = useMutation({
    // The count came through `crewModeView`, which reads this tab's preview flag;
    // the server cannot see it, so the press says it.
    // It names the sessions it counted, so the run never reaches a session that
    // became interrupted after the press (or while its confirmation was open).
    mutationFn: (keys: string[]) => api.chatSlotsResumeAll(crewModeEnabled(), keys),
    onSuccess: (res) => {
      requestOpenRef.current = false
      setError('')
      if (!res.run_id || res.total === 0) {
        earlyRef.current.clear()
        setResult({ ok: true, total: 0, resumed: [], skipped: [], failed: [] })
        return
      }
      follow({ id: res.run_id, total: res.total, adopted: false })
    },
    onError: (e) => {
      requestOpenRef.current = false
      setResult(null)
      const { status, code, runId, total } = refusalOf(e)
      if (status === 409 && code === RESUME_ALL_RUNNING_CODE) {
        // Another press's run: its candidates are not the ones this tab counted.
        pressedKeysRef.current = []
        setResumeAllWaiting([])
        follow({ id: runId, total, adopted: true })
        return
      }
      earlyRef.current.clear()
      setError(resumeAllErrorText(e))
    },
  })
  // Always listening, not only while a run is awaited. The frame of the run
  // this row waits on (matched by id; a 409 with no id to match takes any
  // outcome) settles it. Any other frame with an id is kept by its run id
  // while the request is open, for the 202 or 409 that will name it.
  useEffect(() => {
    const onDone = (ev: Event) => {
      const detail = (ev as CustomEvent<ResumeAllResult>).detail
      if (!detail || typeof detail !== 'object') return
      const current = waitedRef.current
      if (current && (!current.id || detail.run_id === current.id)) {
        waitedRef.current = null
        setWaited(null)
        settle(detail)
        return
      }
      if (!requestOpenRef.current || typeof detail.run_id !== 'string' || !detail.run_id) return
      earlyRef.current.set(detail.run_id, detail)
    }
    window.addEventListener(RESUME_ALL_DONE_EVENT, onDone)
    return () => window.removeEventListener(RESUME_ALL_DONE_EVENT, onDone)
  }, [])
  // Frames are one-shot, and one sent while the socket is down never arrives:
  // never wait while it is down, whether it went down during the wait or before
  // the 202 installed it. A press once it is back gets the 409 while the run is
  // still going, and follows it again.
  useEffect(() => {
    if (connected || !waited) return
    waitedRef.current = null
    setWaited(null)
  }, [connected, waited])
  useEffect(() => {
    if (!waited) return
    const t = setTimeout(() => {
      waitedRef.current = null
      setWaited(null)
    }, resumeAllPendingCeilingMs(waited.total))
    return () => clearTimeout(t)
  }, [waited])
  useEffect(() => {
    if (!result) return
    const t = setTimeout(() => setResult(null), RESUME_ALL_RESULT_MS)
    return () => clearTimeout(t)
  }, [result])
  const pending = mutation.isPending || waited !== null
  useEffect(() => {
    if (pending) {
      const byKey = new Map(localSlots.map(s => [s.key, s]))
      pressedKeysRef.current = pressedKeysRef.current.filter(key => {
        const row = byKey.get(key)
        return !!row && !row.running && !!row.interrupted
      })
    }
    setResumeAllWaiting(pending ? pressedKeysRef.current : [])
  }, [pending, localSlots])
  useEffect(() => () => setResumeAllWaiting([]), [])
  const press = async () => {
    // The sessions this press is about: the ones the button counted, which the
    // confirmation names too. A slots push while it is open changes neither.
    const keys = candidates.map(s => s.key)
    if (keys.length > RESUME_ALL_CONFIRM_ABOVE) {
      const label = i18nT('pages.chatSidebar.resume_all_sessions', { count: keys.length })
      const ok = await confirm({
        title: i18nT('pages.chatSidebar.resume_all_confirm_title'),
        body: i18nT('pages.chatSidebar.resume_all_confirm_body', { limit: RESUME_ALL_MAX_RUNNING }),
        confirmLabel: label,
        danger: false,
        primary: true,
      })
      if (!ok || !connectedRef.current) return
    }
    setError(''); setResult(null); setProblems([])
    pressedKeysRef.current = keys
    requestOpenRef.current = true
    mutation.mutate(keys)
  }
  const titles = useMemo(() => new Map(localSlots.map(s => [s.key, s.title || s.key])), [localSlots])
  const titleOf = (key: string) => titles.get(key) ?? key
  return {
    count,
    result,
    error,
    setError,
    problems,
    dismissProblems: () => setProblems([]),
    titleOf,
    pending,
    alreadyRunning: !!waited?.adopted,
    run: () => { void press() },
    connected,
    confirmDialog,
  }
}

/** One line for the outcome: what resumed, and what did not and why not. */
export function resumeAllResultText(res: ResumeAllResult): string {
  const left = res.skipped.length + res.failed.length
  if (res.total === 0) return i18nT('pages.chatSidebar.resume_all_nothing')
  if (left === 0) return i18nT('pages.chatSidebar.resume_all_done', { count: res.resumed.length })
  return i18nT('pages.chatSidebar.resume_all_partial', { count: res.resumed.length, left })
}

/** The notice naming the sessions a run left for the owner, an alert of its
 *  own. One line per session, so thirty of them would push the shelf past the
 *  panel: the list scrolls in its own box, and the hand-off and Dismiss stay
 *  outside it, always in view. While lines sit below the box's edge, the edge
 *  fades (`MORE_BELOW_MASK`, the model list's cue): a box that ends on a whole
 *  line otherwise reads as the end of the list. askAgent: those sessions stay
 *  interrupted on the server for the agent to look at. */
function ResumeAllProblemsNotice({ text, onDismiss }: { text: string; onDismiss: () => void }) {
  const [attachList, listEdges, remeasureList] = useScrollEdgesY<HTMLSpanElement>()
  // A renamed session or a language switch changes the lines while the box
  // keeps its height, which the hook's observer does not report.
  useEffect(() => { remeasureList() }, [text, remeasureList])
  return (
    <div className="mx-2 mt-2 shrink-0">
      <ErrorNotice
        variant="inline"
        className="flex-wrap w-full"
        title={i18nT('pages.chatSidebar.resume_all_not_resumed')}
        message={text}
        messagePlacement="below"
        messageClassName={listEdges.bottom ? `${RESUME_ALL_PROBLEMS_MESSAGE_CLASS} ${MORE_BELOW_MASK}` : RESUME_ALL_PROBLEMS_MESSAGE_CLASS}
        messageRef={attachList}
        askAgent
        onDismiss={onDismiss}
        testId="resume-all-problems"
      />
    </div>
  )
}

/** The shelf row. Rendered only while there is something to resume, a run in
 *  flight, a result to show, or an error to report -- an idle install carries
 *  no row. A failed request renders through `ErrorNotice` BELOW the status row,
 *  not inside it: the notice is its own alert, and nesting it in the polite
 *  status region would announce it twice. */
export function renderResumeAllRow(shell: SidebarScope): React.ReactNode {
  const {
    count, result, error, setError, problems, dismissProblems, titleOf, pending, alreadyRunning, run, connected, confirmDialog,
  } = shell.resumeAll
  if (count === 0 && !result && !error && !pending && problems.length === 0) return confirmDialog
  const label = i18nT('pages.chatSidebar.resume_all_sessions', { count })
  return (
    <>
      {(count > 0 || result || pending) && (
        <div
          role="status"
          aria-live="polite"
          className="mx-2 mt-2 shrink-0 rounded-lg border border-border bg-bg-elevated px-3 py-2 text-[12px] text-muted"
          data-testid="resume-all-row"
          data-resume-all-count={count}
        >
          <div className="flex items-center gap-2">
            <RotateCcw size={14} className="shrink-0" aria-hidden="true" />
            <span className="min-w-0 flex-1" data-testid="resume-all-text">
              {result
                ? resumeAllResultText(result)
                : alreadyRunning
                  ? i18nT('pages.chatSidebar.resume_all_already_running')
                  : i18nT('pages.chatSidebar.resume_all_interrupted', { count })}
            </span>
          </div>
          {/* On its own line, aligned with the sentence, like the folder-reorder
              hint beside it: beside the text the two fight for the sidebar's width.
              Withheld offline: the press is a request, and a disabled button says
              so where a failing one would only report it afterwards. Disabled
              while a run is in flight, so the row says "Resuming…" rather than
              inviting a second press the server would refuse. That label is a
              state, not a dimmed action: it keeps full opacity in the muted ink,
              which holds its contrast on the shelf in every theme. */}
          {(count > 0 || pending) && (
            <Btn
              className={`mt-1 ml-[22px] p-0 border-none bg-transparent rounded-none hover:bg-transparent text-[12px] font-medium active:scale-100 ${
                pending ? 'text-muted disabled:opacity-100' : 'text-accent hover:underline'
              }`}
              data-testid="resume-all-button"
              disabled={pending || !connected}
              aria-busy={pending || undefined}
              title={label}
              {...(connected ? {} : offlineProps(connected, i18nT('utils.offline.resume_sessions'), label))}
              onClick={run}
            >
              {pending ? i18nT('pages.chatSidebar.resume_all_resuming') : label}
            </Btn>
          )}
        </div>
      )}
      {/* askAgent: the press writes nothing the sidebar holds locally, so the
          hand-off cannot destroy a draft, and the sessions stay interrupted on
          the server for the agent to look at. That holds for the owner gate
          too: the agent still runs for a non-owner and can say who the owner
          is. */}
      {error && (
        <div className="mx-2 mt-2 shrink-0">
          <ErrorNotice
            variant="inline"
            className="flex-wrap w-full"
            title={i18nT('pages.chatSidebar.resume_all_failed')}
            message={error}
            askAgent
            onDismiss={() => setError('')}
            testId="resume-all-error"
          />
        </div>
      )}
      {problems.length > 0 && (
        <ResumeAllProblemsNotice text={resumeAllProblemText(problems, titleOf)} onDismiss={dismissProblems} />
      )}
      {confirmDialog}
    </>
  )
}
