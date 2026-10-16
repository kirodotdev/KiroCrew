/** Resume every interrupted idle session at once: the count the row shows, the
 *  one write (`useResumeAll`), and the shelf row that drives it
 *  (`renderResumeAllRow`).
 *
 *  A gateway restart or a run of backend errors leaves many sessions with a turn
 *  that ended without a reply -- the state behind the composer's Resume button
 *  and the sidebar row's "interrupted" cue. Pressing Resume in each tab is the
 *  chore this row removes: one press calls `POST /api/chat/slots/resume-all`,
 *  which starts a background run of the Continue path on every such session,
 *  four at a time, and answers at once with the run's id. The row stays
 *  pending until the run's outcome arrives as a `resume_all_done` frame (the
 *  rows themselves move with the ordinary slots push meanwhile). The frame can
 *  arrive before the 202 has been read -- a short run finishes in milliseconds
 *  -- so the hook keeps the last few outcomes it heard and settles from them
 *  when the run id turns up. A press while a run is in flight -- another tab's
 *  -- gets 409 `resume_all_running` with that run's id and total; the row then
 *  follows that run and says it is already going.
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
import { Btn } from '../../components/ui'
import { RESUME_ALL_DONE_EVENT } from '../../hooks/useWebSocket'
import { i18nT } from '../../i18n/t'
import { errMessage } from '../../utils/thunkError'
import type { Slot } from './types'
import type { SidebarScope } from './scope'

/** How long the outcome line stays after a run before the row returns to
 *  its count (or disappears, once the slots push has cleared the badge). */
export const RESUME_ALL_RESULT_MS = 6000

/** How long the row waits for a run's outcome frame before giving the button
 *  back. Frames are one-shot: a socket that reconnects mid-run misses it, and
 *  the row must not stay pending forever. Generous: the server's own pacing for
 *  N sessions is about N × 0.4 s. */
export function resumeAllPendingCeilingMs(total: number): number {
  return 60_000 + Math.max(0, total) * 1_000
}

/** The 409 a press gets while another press's run is still going. */
export const RESUME_ALL_RUNNING_CODE = 'resume_all_running'

/** How many outcome frames the hook keeps for a run id it has not seen yet. */
export const RESUME_ALL_RECENT_OUTCOMES = 8

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
 *  non-owner dashboard session cannot fix that by retrying or by asking the
 *  agent, so its notice says so and offers neither. */
export function isOwnerOnlyRefusal(e: unknown): boolean {
  const { status, code } = refusalOf(e)
  return status === 403 && code === 'owner_only'
}

type WaitedRun = { id: string; total: number; adopted: boolean }

/** The row's state: the candidate count, the write, and the run it waits on. */
export function useResumeAll({ localSlots, connected }: { localSlots: Slot[]; connected: boolean }) {
  const candidates = useMemo(() => interruptedIdleSlots(localSlots), [localSlots])
  const count = candidates.length
  // The outcome of the LAST run, shown for a few seconds in place of the
  // count. Kept as data, not prose, so the row renders it in the active
  // language even if the language switches while it is up.
  const [result, setResult] = useState<ResumeAllResult | null>(null)
  const [error, setError] = useState('')
  const [ownerOnly, setOwnerOnly] = useState(false)
  // The run this row waits on: its id, its candidate count, and whether it is
  // another press's run that this press found already going (the 409).
  // The ref is written where the 202/409 body is read, not from render: the
  // outcome frame is a separate macrotask and must see the id at once.
  const [waited, setWaited] = useState<WaitedRun | null>(null)
  const waitedRef = useRef<WaitedRun | null>(null)
  // Outcome frames heard for a run id this row was not (yet) waiting on, in
  // arrival order, each kept until its run's pending ceiling has passed.
  const recentRef = useRef(new Map<string, { res: ResumeAllResult; at: number }>())
  const setWait = (next: WaitedRun | null) => {
    waitedRef.current = next
    setWaited(next)
  }
  const pruneRecent = () => {
    const now = Date.now()
    for (const [id, { res, at }] of recentRef.current) {
      if (now - at > resumeAllPendingCeilingMs(res.total)) recentRef.current.delete(id)
    }
  }
  // Wait on *run*, or settle at once if its outcome frame already arrived.
  const follow = (run: WaitedRun) => {
    pruneRecent()
    const early = run.id ? recentRef.current.get(run.id) : undefined
    if (early) {
      recentRef.current.delete(run.id)
      setWait(null)
      setResult(early.res)
      return
    }
    setWait(run)
  }
  const mutation = useMutation({
    mutationFn: () => api.chatSlotsResumeAll(),
    onSuccess: (res) => {
      setError('')
      if (!res.run_id || res.total === 0) {
        setResult({ ok: true, total: 0, resumed: [], skipped: [], failed: [] })
        return
      }
      follow({ id: res.run_id, total: res.total, adopted: false })
    },
    onError: (e) => {
      setResult(null)
      const { status, code, runId, total } = refusalOf(e)
      if (status === 409 && code === RESUME_ALL_RUNNING_CODE) {
        follow({ id: runId, total, adopted: true })
        return
      }
      const owner = isOwnerOnlyRefusal(e)
      setOwnerOnly(owner)
      setError(owner
        ? i18nT('pages.chatSidebar.resume_all_owner_only')
        : errMessage(e) || i18nT('pages.chatSidebar.resume_all_failed'))
    },
  })
  // Always listening, not only while a run is awaited. The frame of the run
  // this row waits on (matched by id; a 409 with no id to match takes any
  // outcome) settles it. Any other frame with an id is kept, bounded, for a
  // 202 or 409 that has not been read yet.
  useEffect(() => {
    const onDone = (ev: Event) => {
      const detail = (ev as CustomEvent<ResumeAllResult>).detail
      if (!detail || typeof detail !== 'object') return
      const current = waitedRef.current
      if (current && (!current.id || detail.run_id === current.id)) {
        waitedRef.current = null
        setWaited(null)
        setResult(detail)
        return
      }
      if (typeof detail.run_id !== 'string' || !detail.run_id) return
      const recent = recentRef.current
      recent.delete(detail.run_id)
      recent.set(detail.run_id, { res: detail, at: Date.now() })
      while (recent.size > RESUME_ALL_RECENT_OUTCOMES) {
        const oldest = recent.keys().next().value
        if (oldest === undefined) break
        recent.delete(oldest)
      }
    }
    window.addEventListener(RESUME_ALL_DONE_EVENT, onDone)
    return () => window.removeEventListener(RESUME_ALL_DONE_EVENT, onDone)
  }, [])
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
  const press = () => { setError(''); setOwnerOnly(false); setResult(null); mutation.mutate() }
  return {
    count,
    result,
    error,
    ownerOnly,
    setError,
    pending: mutation.isPending || waited !== null,
    alreadyRunning: !!waited?.adopted,
    run: press,
    connected,
  }
}

/** One line for the outcome: what resumed, and what did not and why not. */
export function resumeAllResultText(res: ResumeAllResult): string {
  const left = res.skipped.length + res.failed.length
  if (res.total === 0) return i18nT('pages.chatSidebar.resume_all_nothing')
  if (left === 0) return i18nT('pages.chatSidebar.resume_all_done', { count: res.resumed.length })
  return i18nT('pages.chatSidebar.resume_all_partial', { count: res.resumed.length, left })
}

/** The shelf row. Rendered only while there is something to resume, a run in
 *  flight, a result to show, or an error to report -- an idle install carries
 *  no row. A failed request renders through `ErrorNotice` BELOW the status row,
 *  not inside it: the notice is its own alert, and nesting it in the polite
 *  status region would announce it twice. */
export function renderResumeAllRow(shell: SidebarScope): React.ReactNode {
  const { count, result, error, ownerOnly, setError, pending, alreadyRunning, run, connected } = shell.resumeAll
  if (count === 0 && !result && !error && !pending) return null
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
              inviting a second press the server would refuse. */}
          {(count > 0 || pending) && (
            <Btn
              className="mt-1 ml-[22px] p-0 border-none bg-transparent rounded-none text-accent hover:bg-transparent hover:underline text-[12px] font-medium active:scale-100"
              data-testid="resume-all-button"
              disabled={pending || !connected}
              aria-busy={pending || undefined}
              title={label}
              onClick={run}
            >
              {pending ? i18nT('pages.chatSidebar.resume_all_resuming') : label}
            </Btn>
          )}
        </div>
      )}
      {/* askAgent: the press writes nothing the sidebar holds locally, so the
          hand-off cannot destroy a draft, and the sessions stay interrupted on
          the server for the agent to look at. Not for the owner gate: neither
          the agent nor a retry can make a non-owner the owner. */}
      {error && (
        <div className="mx-2 mt-2 shrink-0">
          <ErrorNotice
            variant="inline"
            className="flex-wrap w-full"
            title={i18nT('pages.chatSidebar.resume_all_failed')}
            message={error}
            askAgent={!ownerOnly}
            onDismiss={() => setError('')}
            testId="resume-all-error"
          />
        </div>
      )}
    </>
  )
}
