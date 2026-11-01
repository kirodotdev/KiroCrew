import { useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronRight, ShieldCheck } from 'lucide-react'
import { api } from '../../api/client'
import { ApiError, isTerminalApprovalRefusal } from '../../api/apiError'
import { useAppSelector } from '../../store'
import { Btn } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'
import { sessionDisplayTitle } from '../../utils/sessionDisplayTitle'
import { useCrewWorkerSummary, crewWorkersQueryKey } from '../../pages/chat/command-center/CrewWorkerList'
import type { ChatSlot, PendingApproval } from '../../types'
import { approvalTitle } from '../../pages/chat/command-center/model'

/** One request's identity: its worker, origin, request id and delivery
 *  (`request_mid` for a native request, `request_instance` for a coordinator
 *  record). A request id can recur, so a replacement under the same id is a new
 *  request: it gets its own card (fresh state) and its own place in the stack. */
const requestKey = (worker: string, approval: PendingApproval): string =>
  [worker, approval.origin || '', approval.request_id, approval.request_mid || '', approval.request_instance || ''].join('\u0000')

/** When this tab first saw each pending request, so the stack is oldest first. */
const firstSeen = new Map<string, number>()

export interface CrewApprovalRequest {
  worker: string
  title: string
  task: string
  approval: PendingApproval & { trust_grantable?: string }
}

/** The pending tool approvals of *root*'s workers, oldest first.
 *
 *  Read from the live slot rows, so a request decided anywhere else (the worker's
 *  own chat, another window) leaves the stack on the next slot frame. The task
 *  line is the worker list's (its ledger item's summary or title, redacted on the
 *  server). Exported for tests. */
export function crewApprovalRequests(
  slots: readonly ChatSlot[],
  root: string,
  tasks: ReadonlyMap<string, string>,
  now: number = Date.now(),
): CrewApprovalRequest[] {
  const out: CrewApprovalRequest[] = []
  for (const slot of slots) {
    const info = slot.pending_approval_info
    if (slot.crew_root !== root || !slot.pending_approval || !info?.request_id) continue
    const id = requestKey(slot.key, info)
    if (!firstSeen.has(id)) firstSeen.set(id, now)
    out.push({ worker: slot.key, title: sessionDisplayTitle(slot), task: tasks.get(slot.key) ?? '', approval: info })
  }
  out.sort((a, b) =>
    (firstSeen.get(requestKey(a.worker, a.approval)) ?? 0)
    - (firstSeen.get(requestKey(b.worker, b.approval)) ?? 0))
  return out
}

/**
 * The Crew Mode conductor's approval card stack, above its composer.
 *
 * A worker never asks the person anything: its questions go to its conductor. A
 * tool approval is the one exception, because only the person may grant it. Each
 * waiting request gets a small card naming the worker, what it is working on and
 * the exact tool call, with Approve, Reject and Trust this worker. The decision
 * runs the existing per-slot approve route on the WORKER's own slot, through the
 * root's owner-only crew route (`POST .../crew-workers/{worker}/approve`).
 */
/** `compact` (Ultra Mode Needs your input): each request is one short row --
 *  who and the call in plain words, Approve / Reject, a small Trust link -- with the
 *  full tool input behind a collapsed Details disclosure. Same data, same route. */
export default function CrewApprovalCards({ root, compact = false }: { root: string; compact?: boolean }) {
  const slots = useAppSelector(s => s.dashboard.slots)
  // The last list read, kept through a failed re-read: each card keeps its task
  // line, and the notice below says the newest read did not land.
  const { lastWorkers, failed } = useCrewWorkerSummary(root)
  const tasks = new Map((lastWorkers ?? []).map(w => [w.key, w.summary ?? ''] as const))
  const requests = crewApprovalRequests(slots, root, tasks)
  if (requests.length === 0) return null
  return (
    <section
      aria-label={i18nT('components.crewMode.approvals_label', { n: requests.length })}
      className={compact ? 'space-y-1.5' : 'mx-2 mb-2 space-y-2 max-h-[45vh] overflow-y-auto'}
      data-testid="crew-approval-cards"
      data-compact={compact ? 'true' : undefined}
    >
      {/* The task lines come from the worker list, the last one read; when the
          newest read failed this says so, and the cards still show the exact
          tool input, so the person can decide. Nothing to lose on hand-off: the
          requests are on the server and the draft is saved per chat. */}
      {failed && <ErrorNotice variant="inline" message={i18nT('commandCenter.workers_error')} askAgent testId="crew-approval-workers-error" />}
      {requests.map(r => <CrewApprovalCard key={requestKey(r.worker, r.approval)} root={root} request={r} compact={compact} />)}
    </section>
  )
}

function CrewApprovalCard({ root, request, compact = false }: { root: string; request: CrewApprovalRequest; compact?: boolean }) {
  const queryClient = useQueryClient()
  const [done, setDone] = useState(false)
  const locked = useRef(false)
  const { worker, title, task, approval } = request
  // The whole input the server sent, never a cut: Approve authorizes all of it,
  // so the person must be able to read all of it (the region scrolls).
  const input = approval.tool_input || ''
  const headingId = `crew-approval-${worker}-${approval.request_id}`
  const trustHintId = `${headingId}-trust-hint`
  const mutation = useMutation({
    retry: false,
    mutationFn: (action: 'approve' | 'reject' | 'trust') =>
      api.crewWorkerApprove(root, worker, action, approval.request_id, approval.request_mid || '', approval.request_instance || ''),
    onSuccess: () => setDone(true),
    onSettled: () => {
      locked.current = false
      void queryClient.invalidateQueries({ queryKey: crewWorkersQueryKey(root) })
    },
  })
  // The request is no longer pending on the server (decided elsewhere, or expired):
  // nothing this card can send will land, so its buttons go.
  const gone = isTerminalApprovalRefusal(mutation.error)
  const decide = (action: 'approve' | 'reject' | 'trust') => {
    if (locked.current || done || gone) return
    locked.current = true
    mutation.mutate(action)
  }
  if (done) return null
  if (compact) {
    const busy = mutation.isPending || gone
    // An MCP tool ("@server/tool_name") reads as its tool's own name: its raw
    // arguments can carry session keys, which belong behind Details.
    const mcpTool = (approval.tool || '').match(/^@[^/]+\/(.+)$/)
    const call = mcpTool ? mcpTool[1].replace(/_/g, ' ') : approvalTitle({ id: approval.request_id, tool: approval.tool, tool_input: approval.tool_input, tool_purpose: approval.tool_purpose }) || approval.tool || i18nT('components.crewMode.approval_tool_unknown')
    const small = 'inline-flex items-center justify-center h-7 px-2.5 rounded-md text-[12px] font-medium border transition-colors disabled:opacity-40 shrink-0'
    return (
      <article aria-labelledby={headingId} className="rounded-lg border border-warn bg-card px-2.5 py-2 space-y-1 text-text" data-testid="crew-approval-card" data-worker={worker}>
        <div className="flex items-center gap-2 min-w-0">
          <ShieldCheck size={14} aria-hidden="true" className="shrink-0 text-warn" />
          <h3 id={headingId} className="flex-1 min-w-0 text-[13px] truncate" title={`${title}: ${call}`}>
            <span className="font-semibold">{title}</span><span className="text-muted">: </span><span>{call}</span>
          </h3>
          <button type="button" className={`${small} bg-accent text-accent-fg border-accent`} disabled={busy} onClick={() => decide('approve')}>{i18nT('components.crewMode.approval_approve')}</button>
          <button type="button" className={`${small} border-border hover:bg-bg-hover`} disabled={busy} onClick={() => decide('reject')}>{i18nT('components.crewMode.approval_reject')}</button>
        </div>
        <div className="flex items-start gap-3 pl-[22px] text-[11px] text-muted">
          <details className="group min-w-0 flex-1" data-testid="crew-approval-details">
            <summary className="inline-flex items-center gap-0.5 cursor-pointer list-none hover:text-text">
              <ChevronRight size={12} aria-hidden="true" className="transition-transform group-open:rotate-90" />{i18nT('components.crewMode.ultra_details')}
            </summary>
            {task && <p className="mt-1 text-text/80 break-words">{task}</p>}
            <p className="mt-1 text-text/80"><span className="font-semibold">{approval.tool || i18nT('components.crewMode.approval_tool_unknown')}</span>{approval.tool_purpose ? ` · ${approval.tool_purpose}` : ''}</p>
            {input && (
              // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users scroll the exact input.
              <pre tabIndex={0} role="region" aria-label={i18nT('components.crewMode.approval_input_label')} className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-all bg-bg-hover rounded px-2 py-1 font-mono text-[11px] text-text">{input}</pre>
            )}
          </details>
          {approval.trust_grantable === '1' && (
            <button type="button" className="shrink-0 hover:underline underline-offset-2 hover:text-text disabled:opacity-40" disabled={busy}
              title={i18nT('components.crewMode.approval_trust_hint')} onClick={() => decide('trust')}>{i18nT('components.crewMode.ultra_trust')}</button>
          )}
        </div>
        {/* The same failure surface and wording as the full card below. */}
        <ErrorNotice
          variant="inline"
          message={!mutation.error ? undefined
            : gone ? i18nT('components.approvalCard.approval_no_longer_pending')
              : mutation.error instanceof ApiError && mutation.error.message
                ? i18nT('components.approvalCard.decision_not_recorded_error', { error: mutation.error.message })
                : i18nT('components.approvalCard.decision_failed')}
          askAgent
          testId="crew-approval-failure"
        />
      </article>
    )
  }
  return (
    <article
      aria-labelledby={headingId}
      className="rounded-lg border border-warn bg-card p-3 space-y-2 text-text"
      data-testid="crew-approval-card"
      data-worker={worker}
    >
      <h3 id={headingId} className="flex items-center gap-1.5 text-[13px] font-semibold min-w-0">
        <ShieldCheck size={15} aria-hidden="true" className="shrink-0 text-warn" />
        <span className="truncate">{i18nT('components.crewMode.approval_title', { name: title })}</span>
      </h3>
      {task && <p className="text-[12px] text-muted break-words line-clamp-2">{task}</p>}
      <p className="text-[12px] break-words">
        <span className="font-semibold">{approval.tool || i18nT('components.crewMode.approval_tool_unknown')}</span>
        {approval.tool_purpose ? <span className="text-muted"> · {approval.tool_purpose}</span> : null}
      </p>
      {input && (
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex -- keyboard users scroll the exact command.
        <pre tabIndex={0} role="region" aria-label={i18nT('components.crewMode.approval_input_label')}
          className="max-w-full max-h-40 overflow-auto whitespace-pre-wrap break-all bg-bg-hover rounded-md px-2 py-1.5 text-[12px] font-mono"
        >{input}</pre>
      )}
      {/* A failed decision with nothing to lose: the request is already on the
          server and the composer's draft is saved per chat (utils/chatDrafts),
          so the hand-off may navigate. The wording is the per-slot card's
          (ApprovalCard): a refusal that proves the request gone (404, "no pending
          approval") says so and takes the buttons away -- the next slot frame
          drops the card; a refusal with the server's own message quotes it (a
          sign-in that lapsed included; the client's silent refresh retries it);
          a transport failure with no answer says the decision may not have
          landed, and the buttons stay for one more try. */}
      <ErrorNotice
        message={!mutation.error ? undefined
          : gone ? i18nT('components.approvalCard.approval_no_longer_pending')
            : mutation.error instanceof ApiError && mutation.error.message
              ? i18nT('components.approvalCard.decision_not_recorded_error', { error: mutation.error.message })
              : i18nT('components.approvalCard.decision_failed')}
        askAgent
        testId="crew-approval-failure"
      />
      {/* The two answers to THIS request in one row (primary + secondary). */}
      <div className="flex flex-wrap gap-2">
        <Btn primary className="min-h-11" disabled={mutation.isPending || gone} onClick={() => decide('approve')}>
          {i18nT('components.crewMode.approval_approve')}
        </Btn>
        <Btn className="min-h-11" disabled={mutation.isPending || gone} onClick={() => decide('reject')}>
          {i18nT('components.crewMode.approval_reject')}
        </Btn>
      </div>
      {/* Trust reaches past this request, so it sits on its own row with what it
          grants written beside it, read by everyone and not only on hover. */}
      {approval.trust_grantable === '1' && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1" data-testid="crew-approval-trust-row">
          <Btn className="min-h-11" disabled={mutation.isPending || gone} onClick={() => decide('trust')} aria-describedby={trustHintId}>
            {i18nT('components.crewMode.approval_trust')}
          </Btn>
          <p id={trustHintId} className="text-[11px] text-muted min-w-0 flex-1 basis-40">{i18nT('components.crewMode.approval_trust_hint')}</p>
        </div>
      )}
    </article>
  )
}
