/**
 * Settings → Remote Crew → "Your crews": Stop / Resume / Idle timeout / Rename / Delete on the
 * row of each crew this lane made, through the core's row-action seam
 * (`registerCrewRowActions`, keyed by the row's `provisioner_id` stamp, `kiro_managed_env`).
 *
 * Both components first read the lane switch from `GET /api/managed-environments/row-lifecycle`,
 * which makes no service call. Only once it reads on do they read the list answer
 * (`GET /api/managed-environments/list`, the query the launch form runs too) and find the
 * row's environment by `instance_id`. So with the lane off "Your crews" looks as before and
 * makes no list call. Every action calls `POST /api/managed-environments/{id}/{action}`, which
 * runs the owner check and the guard on the server; drawing a button here grants nothing.
 *
 * - `menuItems`: Stop (only while RUNNING), Resume (only while STOPPED), Idle timeout…,
 *   Rename… and Delete… (never while PENDING or GONE), none while an action on that
 *   environment is running, and none on one another client made (`sandboxActionsFor`).
 * - `detail`: the environment's state under the row and the last action's error. It also runs
 *   the actions: the core unmounts the menu as soon as an item is picked, so an item only asks,
 *   and the detail block, which stays mounted with the row, asks Delete's question and posts.
 *
 * The connect:
 * - Once signed in, a row whose last connect failed gets one connect at once; only a list that
 *   says the environment is in a state other than RUNNING holds it back.
 * - While any row reads connecting, one page timer re-reads the core's crews query (and the
 *   rows' connect phases) every 2 s, for 60 s at most; it never re-reads the list, whose every
 *   read is a service call.
 * - A connecting row shows how far it has got, read from `GET .../{id}/connect-phase` (the
 *   dashboard's memory) by the id in the row's own `crew-sbx-<id>` host.
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { QueryClient } from '@tanstack/react-query'
import { Pencil, Play, Power, Timer, Trash2 } from 'lucide-react'
import { DropdownMenuItem, DropdownMenuSeparator } from '../ui/dropdown-menu'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'
import { useConfirm } from '../ConfirmDialog'
import type { CrewRowActionProps } from '../remoteCrewRowActions'
import { api } from '../../api/client'
import type { InstanceView } from '../../api/client'
import { managedEnvApi } from '../../api/managedEnvApi'
import type { SandboxAction, SandboxConnectPhase, SandboxItem, SandboxList } from '../../api/managedEnvApi'
import {
  SANDBOX_LIST_QUERY_KEY,
  sandboxActionsFor,
  sandboxPollMs,
  sandboxRowLifecycle,
  sandboxSignedIn,
  sandboxSignedInLine,
  useCrewsReadAfterRowsAdded,
  useSandboxRouteState,
  useSandboxSignedInUser,
} from './SandboxLaunchForm'
import SandboxIdleTimeoutDialog, { idleLine } from './SandboxIdleTimeoutDialog'
import SandboxRenameDialog from './SandboxRenameDialog'
import type { RenameFailure } from './SandboxRenameDialog'
import { ROW_TEXT, rowActionLabel, rowDeleteTitle, rowStatus } from './text'

/** The `provisioner_id` stamp on this lane's rows: kiro_crew/managed_env PROVISIONER_ID, verbatim. */
export const SANDBOX_PROVISIONER_ID = 'kiro_managed_env'

/** The core's crews list (RemoteCrewPanel's `['instances']` query), re-read once a Delete is done. */
const CREWS_QUERY_KEY = ['instances'] as const

/** While a row connects, the crews query is re-read this often, for at most the cap. */
export const CONNECT_REREAD_MS = 2_000
export const CONNECT_REREAD_CAP_MS = 60_000
/** One entry per environment id: `[...SANDBOX_CONNECT_PHASE_QUERY_KEY, id]`. */
export const SANDBOX_CONNECT_PHASE_QUERY_KEY = ['managed-env-connect-phase'] as const

export function connectPhaseText(phase: SandboxConnectPhase['phase'] | undefined): string {
  if (phase === 'starting') return ROW_TEXT.phaseStarting
  if (phase === 'tunnel') return ROW_TEXT.phaseTunnel
  if (phase === 'signing_in') return ROW_TEXT.phaseSigningIn
  return ''
}

/** Same rule as engine.py's `sandbox_id_from_host`: the id in `crew-sbx-<id>`, or ''. */
const SANDBOX_HOST_PREFIX = 'crew-sbx-'
const SANDBOX_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

export function sandboxIdFromHost(host: string | undefined): string {
  if (!host || !host.startsWith(SANDBOX_HOST_PREFIX)) return ''
  const sid = host.slice(SANDBOX_HOST_PREFIX.length)
  return SANDBOX_ID_RE.test(sid) ? sid : ''
}

// One re-read timer for the page, whatever the number of connecting rows. It starts when the
// first row reads connecting and stops when none does, or at the cap; after the cap it waits for
// every row to leave connecting before a new connect can start it again.
const connectingRows = new Set<string>()
const reread: {
  timer: ReturnType<typeof setInterval> | undefined
  startedAt: number
  capped: boolean
  qc: QueryClient | undefined
} = { timer: undefined, startedAt: 0, capped: false, qc: undefined }

function stopReread(): void {
  if (reread.timer !== undefined) clearInterval(reread.timer)
  reread.timer = undefined
}

function rereadTick(): void {
  if (Date.now() - reread.startedAt >= CONNECT_REREAD_CAP_MS) {
    stopReread()
    reread.capped = true
    return
  }
  // The core's crews query and the rows' phases: never the list (a service call each).
  void reread.qc?.invalidateQueries({ queryKey: CREWS_QUERY_KEY })
  void reread.qc?.invalidateQueries({ queryKey: SANDBOX_CONNECT_PHASE_QUERY_KEY })
}

function syncReread(): void {
  if (connectingRows.size === 0) {
    stopReread()
    reread.capped = false
    return
  }
  if (reread.timer !== undefined || reread.capped) return
  reread.startedAt = Date.now()
  reread.timer = setInterval(rereadTick, CONNECT_REREAD_MS)
}

function useConnectReread(qc: QueryClient, instanceId: string, connecting: boolean): void {
  useEffect(() => {
    if (!connecting) return
    reread.qc = qc
    connectingRows.add(instanceId)
    syncReread()
    return () => {
      connectingRows.delete(instanceId)
      syncReread()
    }
  }, [qc, instanceId, connecting])
}

function inProgressText(action: string): string {
  if (action === 'stop') return ROW_TEXT.stopping
  if (action === 'resume') return ROW_TEXT.resuming
  if (action === 'delete') return ROW_TEXT.deleting
  if (action === 'idle-timeout') return ROW_TEXT.idleChanging
  return ROW_TEXT.inProgress
}

/** The core row's tunnel is up or on its way up (the states its Disconnect ends). */
export function rowConnectionUp(inst: InstanceView): boolean {
  const state = inst.status?.state
  return state === 'connected' || state === 'connecting'
}

/** Idle timeout… is offered on an own environment the list still has (not GONE), and, like every
 * row action, not while another action on it runs. */
export function sandboxIdleOffered(item: SandboxItem): boolean {
  return item.own !== false && item.status !== 'GONE' && Boolean(item.status) && !item.action?.running
}

/** The list item of this crew's environment, or undefined (lane off, no list yet, or no match). */
export function sandboxForRow(list: SandboxList | undefined, instanceId: string): SandboxItem | undefined {
  if (!sandboxRowLifecycle(list) || !instanceId) return undefined
  return list?.sandboxes.find(i => i.instance_id === instanceId)
}

/** The detail block's state line. */
export function sandboxRowStateText(item: SandboxItem): string {
  if (item.action?.running) return inProgressText(item.action.action)
  if (item.status === 'RUNNING') return ROW_TEXT.running
  if (item.status === 'STOPPED') return ROW_TEXT.stopped
  if (!item.status) return ROW_TEXT.unknown
  return rowStatus(item.status)
}

// A menu item's pick, handed to the detail block of the same row (the menu is gone by then).
type RowPick = SandboxAction | 'idle' | 'rename'

/** Rename is on a crew's row while the List decides (`crews_from_list`), on a box that is the
 * user's own (the ledger or the accepted set), while no action runs on it. */
export function sandboxRenameOffered(item: SandboxItem, list: SandboxList | undefined): boolean {
  return list?.crews_from_list === true && item.own !== false && !item.action?.running
}
type RowRequest = (act: RowPick) => void
const handlers = new Map<string, RowRequest>()

function requestRowAction(instanceId: string, act: RowPick): void {
  handlers.get(instanceId)?.(act)
}

export { SANDBOX_ROW_LIFECYCLE_QUERY_KEY } from './SandboxLaunchForm'

function useRowLifecycleOn(): boolean {
  return useSandboxRouteState().data?.row_lifecycle === true
}

/** The list query both components share with the form: one request, one cache entry. It runs
 * only once the lane reads on, so with it off "Your crews" makes no list call at all. */
function useSandboxList(fast: boolean) {
  const on = useRowLifecycleOn()
  const list = useQuery({
    queryKey: SANDBOX_LIST_QUERY_KEY,
    queryFn: () => managedEnvApi.sandboxList(),
    enabled: on,
    // Keyed on the switch route's answer, not on the list's own data: a first read that failed
    // (before sign-in) has no data, and the row must still recover at the next poll or on focus.
    refetchInterval: q => {
      if (!on) return false
      return fast ? 3_000 : sandboxPollMs(q.state.data?.sandboxes)
    },
    refetchOnWindowFocus: on,
  })
  useCrewsReadAfterRowsAdded(list.data, list.dataUpdatedAt)
  return list
}

export function SandboxCrewRowMenuItems({ inst, disabled }: CrewRowActionProps) {
  const list = useSandboxList(false)
  // Who the lane is signed in as, so a user can tell one account from another.
  const signedIn = sandboxSignedIn(useSandboxRouteState().data)
  const user = useSandboxSignedInUser(signedIn)
  const item = sandboxForRow(list.data, inst.id)
  if (!item) return null
  const can = sandboxActionsFor(item)
  const idle = sandboxIdleOffered(item)
  const rename = sandboxRenameOffered(item, list.data)
  if (!can.stop && !can.resume && !can.delete && !idle && !rename) return null
  return (
    <>
      <DropdownMenuSeparator />
      {signedIn ? (
        <div className="px-2 py-1 text-[12px] text-muted" data-testid={`sandbox-row-signed-in-${inst.id}`}>
          {sandboxSignedInLine(user.data)}
        </div>
      ) : null}
      {can.stop ? (
        <DropdownMenuItem
          className="gap-2 text-[13px]"
          disabled={disabled}
          onSelect={() => requestRowAction(inst.id, 'stop')}
          aria-label={rowActionLabel('stop', inst.name)}
          data-testid={`sandbox-row-stop-${inst.id}`}
        >
          <Power className="lucide-inline" /> {ROW_TEXT.stop}
        </DropdownMenuItem>
      ) : null}
      {can.resume ? (
        <DropdownMenuItem
          className="gap-2 text-[13px]"
          disabled={disabled}
          onSelect={() => requestRowAction(inst.id, 'resume')}
          aria-label={rowActionLabel('resume', inst.name)}
          data-testid={`sandbox-row-resume-${inst.id}`}
        >
          <Play className="lucide-inline" /> {ROW_TEXT.resume}
        </DropdownMenuItem>
      ) : null}
      {idle ? (
        <DropdownMenuItem
          className="gap-2 text-[13px]"
          // greyed for 10 minutes after the idle switch's answer, which no change can pass
          disabled={disabled || list.data?.idle_off === true}
          onSelect={() => requestRowAction(inst.id, 'idle')}
          aria-label={rowActionLabel('idle', inst.name)}
          data-testid={`sandbox-row-idle-item-${inst.id}`}
        >
          <Timer className="lucide-inline" /> {ROW_TEXT.idle}
        </DropdownMenuItem>
      ) : null}
      {rename ? (
        <DropdownMenuItem
          className="gap-2 text-[13px]"
          disabled={disabled}
          onSelect={() => requestRowAction(inst.id, 'rename')}
          aria-label={rowActionLabel('rename', inst.name)}
          data-testid={`sandbox-row-rename-item-${inst.id}`}
        >
          <Pencil className="lucide-inline" /> {ROW_TEXT.rename}
        </DropdownMenuItem>
      ) : null}
      {can.delete ? (
        <DropdownMenuItem
          className="gap-2 text-[13px] text-danger"
          disabled={disabled}
          onSelect={() => requestRowAction(inst.id, 'delete')}
          aria-label={rowActionLabel('delete', inst.name)}
          data-testid={`sandbox-row-delete-${inst.id}`}
        >
          <Trash2 className="lucide-inline" /> {ROW_TEXT.delete}
        </DropdownMenuItem>
      ) : null}
    </>
  )
}

export function SandboxCrewRowDetail({ inst, disabled }: CrewRowActionProps) {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  // A Delete was accepted (a 202: it runs on): poll fast until it ends.
  const [deleting, setDeleting] = useState(false)
  const list = useSandboxList(deleting)
  const item = sandboxForRow(list.data, inst.id)
  // A Resume was accepted: connect once it is RUNNING (the row's Stop disconnected it first).
  const [resumed, setResumed] = useState(false)
  const action = useMutation({
    mutationFn: async ({ sandboxId, act }: { sandboxId: string; act: SandboxAction }) => {
      // The row's Stop disconnects the crew first, so nothing retries a connect while STOPPED.
      if (act === 'stop' && rowConnectionUp(latest.current.inst)) {
        await Promise.resolve(api.disconnectInstance(latest.current.inst.id)).catch(() => undefined)
        void qc.invalidateQueries({ queryKey: CREWS_QUERY_KEY })
      }
      return managedEnvApi.sandboxAction(sandboxId, act)
    },
    onSuccess: (_res, { act }) => {
      if (act === 'delete') setDeleting(true)
      if (act === 'resume') setResumed(true)
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY }),
  })
  // Crew's own sign-in (Settings → Kiro account), token-free, from the switch's own route.
  const route = useSandboxRouteState(true)
  const [idleOpen, setIdleOpen] = useState(false)
  const idleOff = list.data?.idle_off === true
  // Start Crew is one run of the box's hook (the route), then the one Connect, the core's own,
  // only after a start. Nothing repeats without a new click.
  const startCrew = useMutation({
    mutationFn: (sandboxId: string) => managedEnvApi.sandboxStartCrew(sandboxId),
    onSuccess: res => {
      if (!res.started) return
      void Promise.resolve(api.connectInstance(latest.current.inst.id))
        .catch(() => undefined)
        .finally(() => void qc.invalidateQueries({ queryKey: CREWS_QUERY_KEY }))
    },
  })
  const idleSave = useMutation({
    mutationFn: ({ sandboxId, minutes }: { sandboxId: string; minutes: number }) =>
      managedEnvApi.sandboxIdleTimeout(sandboxId, minutes),
    onSuccess: () => setIdleOpen(false),
    onSettled: () => void qc.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY }),
  })
  // Rename…: a failure (an answer with a line, or a refused post) shows in the dialog, which
  // stays open; nothing stays on the row. A rename with no line closes it, and the crews list
  // is re-read, since the row's name may have changed.
  const [renameOpen, setRenameOpen] = useState(false)
  // an answer that lands after Cancel sets no failure, so the next open is clean
  const renameOpenRef = useRef(false)
  renameOpenRef.current = renameOpen
  const [renameFailure, setRenameFailure] = useState<RenameFailure | null>(null)
  const failRename = (text: string) => {
    if (renameOpenRef.current) setRenameFailure(f => ({ text, seq: (f?.seq ?? 0) + 1 }))
  }
  const renameSave = useMutation({
    mutationFn: ({ sandboxId, name }: { sandboxId: string; name: string }) =>
      managedEnvApi.sandboxRename(sandboxId, name),
    onMutate: () => setRenameFailure(null),
    onSuccess: res => {
      if (res.line) {
        failRename(res.line)
      } else {
        setRenameOpen(false)
      }
      void qc.invalidateQueries({ queryKey: CREWS_QUERY_KEY })
    },
    onError: err => failRename(err instanceof Error ? err.message : String(err)),
    onSettled: () => void qc.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY }),
  })

  // The handler reads the newest state through a ref: the menu's pick arrives between renders,
  // and Delete's answer can come any time later, so the rules are checked again after it.
  const latest = useRef({ item, disabled, pending: action.isPending, idleOff, inst, list: list.data })
  latest.current = { item, disabled, pending: action.isPending, idleOff, inst, list: list.data }
  const { mutate } = action
  useEffect(() => {
    const allowed = (act: SandboxAction): SandboxItem | undefined => {
      const { item: cur, disabled: off, pending } = latest.current
      return cur && !off && !pending && sandboxActionsFor(cur)[act] ? cur : undefined
    }
    const onRequest: RowRequest = async act => {
      if (act === 'idle') {
        const { item: cur, disabled: off, idleOff: greyed } = latest.current
        if (cur && !off && !greyed && sandboxIdleOffered(cur)) setIdleOpen(true)
        return
      }
      if (act === 'rename') {
        const { item: cur, disabled: off, list: answer } = latest.current
        if (cur && !off && sandboxRenameOffered(cur, answer)) {
          renameOpenRef.current = true
          setRenameOpen(true)
        }
        return
      }
      if (!allowed(act)) return
      if (act === 'delete') {
        const ok = await confirm({
          title: rowDeleteTitle(inst.name),
          body: <p>{ROW_TEXT.deleteBody}</p>,
          confirmLabel: ROW_TEXT.deleteConfirm,
          danger: true,
        })
        if (!ok) return
      }
      const cur = allowed(act)
      if (cur) mutate({ sandboxId: cur.sandbox_id, act })
    }
    handlers.set(inst.id, onRequest)
    return () => {
      if (handlers.get(inst.id) === onRequest) handlers.delete(inst.id)
    }
  }, [inst.id, inst.name, confirm, mutate])

  // The Delete is over once the list no longer ties an environment to this row (the server
  // dropped the row) or the delete ended with an error. Either way the crews list is read again.
  const deleteEnded = deleting && (!item || Boolean(item.action && !item.action.running && item.action.error))
  useEffect(() => {
    if (!deleteEnded) return
    setDeleting(false)
    void qc.invalidateQueries({ queryKey: CREWS_QUERY_KEY })
  }, [deleteEnded, qc])

  // One connect, the call the row's Connect makes, when the crew should be reachable and isn't:
  // (a) signed in and the row's last connect failed, unless the list says the environment is in
  // a state other than RUNNING; (b) a Resume this row asked for brought it back to RUNNING.
  const connectTried = useRef(false)
  const rowOn = route.data?.row_lifecycle === true
  const signedIn = sandboxSignedIn(route.data)
  const listSaysNotRunning = Boolean(item?.status) && item?.status !== 'RUNNING'
  const staleError =
    rowOn &&
    signedIn &&
    !listSaysNotRunning &&
    !item?.action?.running &&
    !inst.disabled &&
    !rowConnectionUp(inst) &&
    Boolean(inst.status?.error)
  const resumeDone = resumed && item?.status === 'RUNNING' && !item.action?.running
  const resumeFailed = resumed && Boolean(item?.action && !item.action.running && item.action.error)
  useEffect(() => {
    if (resumeFailed) setResumed(false)
  }, [resumeFailed])
  useEffect(() => {
    const viaResume = resumeDone && !item?.action?.error
    if (!(viaResume || (staleError && !connectTried.current))) return
    connectTried.current = true
    if (viaResume) setResumed(false)
    if (inst.disabled) return
    void Promise.resolve(api.connectInstance(inst.id))
      .catch(() => undefined)
      .finally(() => void qc.invalidateQueries({ queryKey: CREWS_QUERY_KEY }))
  }, [staleError, resumeDone, item?.action?.error, inst.id, inst.disabled, qc])

  // While the row connects: the page's one re-read timer, and how far the connect has got, by
  // the environment id in the row's own host (never the list), re-read on that timer's tick.
  const connecting = rowOn && !inst.disabled && inst.status?.state === 'connecting'
  useConnectReread(qc, inst.id, connecting)
  const phaseSid = sandboxIdFromHost(inst.ssh_host)
  const phase = useQuery({
    queryKey: [...SANDBOX_CONNECT_PHASE_QUERY_KEY, phaseSid],
    queryFn: () => managedEnvApi.sandboxConnectPhase(phaseSid),
    enabled: connecting && Boolean(phaseSid),
    refetchInterval: false,
    refetchOnWindowFocus: false,
    retry: false,
  })
  const phaseText = connecting ? connectPhaseText(phase.data?.phase) : ''
  const phaseLine = phaseText ? (
    <div className="text-[12px] text-muted" data-testid={`sandbox-row-phase-${inst.id}`}>
      {phaseText}
    </div>
  ) : null

  if (!item) {
    return phaseLine ? (
      <div className="mt-1.5 pl-11 space-y-1" data-testid={`sandbox-row-detail-${inst.id}`}>
        {confirmDialog}
        {phaseLine}
      </div>
    ) : (
      confirmDialog
    )
  }
  const lastError = item.action && !item.action.running ? item.action.error : ''
  const note = item.action && !item.action.running ? item.action.note ?? '' : ''
  const idleText = idleLine(item, idleOff)
  // The organization's policy refused this box's connect: its own plain line says why.
  const policyLine = item.policy_line ?? ''
  const unreachable = item.status === 'RUNNING' && !inst.disabled && Boolean(inst.status?.error) && !policyLine
  // the guard has stopped the client, so no reconnect can work until a restart
  const cantReconnect = list.data?.client_stopped === true && !inst.disabled && Boolean(inst.status?.error)
  const startLine = startCrew.data && !startCrew.data.started ? startCrew.data.line : ''
  // The row's own buttons: Stop, Resume and Idle timeout… in plain sight, by the same rules as
  // the menu items, through the same handler. Delete stays in the menu only, behind its question.
  const can = sandboxActionsFor(item)
  const idleShown = sandboxIdleOffered(item)
  const busy = disabled || action.isPending
  return (
    <div className="mt-1.5 pl-11 space-y-1" data-testid={`sandbox-row-detail-${inst.id}`}>
      {confirmDialog}
      <SandboxRenameDialog
        sandboxId={item.sandbox_id}
        crewName={inst.name}
        open={renameOpen}
        busy={renameSave.isPending}
        failure={renameFailure}
        onClose={() => {
          renameOpenRef.current = false
          setRenameOpen(false)
          setRenameFailure(null)
        }}
        onSave={name => renameSave.mutate({ sandboxId: item.sandbox_id, name })}
      />
      <SandboxIdleTimeoutDialog
        item={item}
        crewName={inst.name}
        open={idleOpen}
        busy={idleSave.isPending}
        onClose={() => setIdleOpen(false)}
        onSave={minutes => idleSave.mutate({ sandboxId: item.sandbox_id, minutes })}
      />
      <div className="text-[12px] text-muted" data-testid={`sandbox-row-state-${inst.id}`}>
        {sandboxRowStateText(item)}
      </div>
      {phaseLine}
      {can.stop || can.resume || idleShown ? (
        <div className="flex flex-wrap gap-2" data-testid={`sandbox-row-buttons-${inst.id}`}>
          {can.stop ? (
            <Btn
              onClick={() => requestRowAction(inst.id, 'stop')}
              disabled={busy}
              aria-label={rowActionLabel('stop', inst.name)}
              data-testid={`sandbox-row-stop-btn-${inst.id}`}
            >
              {ROW_TEXT.stopButton}
            </Btn>
          ) : null}
          {can.resume ? (
            <Btn
              onClick={() => requestRowAction(inst.id, 'resume')}
              disabled={busy}
              aria-label={rowActionLabel('resume', inst.name)}
              data-testid={`sandbox-row-resume-btn-${inst.id}`}
            >
              {ROW_TEXT.resumeButton}
            </Btn>
          ) : null}
          {idleShown ? (
            <Btn
              onClick={() => requestRowAction(inst.id, 'idle')}
              // greyed for 10 minutes after the idle switch's answer, as the menu item is
              disabled={disabled || idleOff}
              aria-label={rowActionLabel('idle', inst.name)}
              data-testid={`sandbox-row-idle-btn-${inst.id}`}
            >
              {ROW_TEXT.idleButton}
            </Btn>
          ) : null}
        </div>
      ) : null}
      {policyLine ? (
        <div className="text-[12px] text-danger" data-testid={`sandbox-row-policy-${inst.id}`}>
          {policyLine}
        </div>
      ) : null}
      {unreachable ? (
        <div className="text-[12px] text-muted" data-testid={`sandbox-row-unreachable-${inst.id}`}>
          {ROW_TEXT.unreachable}
        </div>
      ) : null}
      {cantReconnect ? (
        <div className="text-[12px] text-muted" data-testid={`sandbox-row-cant-reconnect-${inst.id}`}>
          {ROW_TEXT.cantReconnect}
        </div>
      ) : null}
      {unreachable && item.own !== false && list.data?.crews_from_list === true ? (
        <div className="flex flex-wrap items-center gap-2" data-testid={`sandbox-row-gateway-down-${inst.id}`}>
          <span className="text-[12px] text-muted">{ROW_TEXT.gatewayDown}</span>
          <Btn
            onClick={() => startCrew.mutate(item.sandbox_id)}
            disabled={disabled || startCrew.isPending}
            data-testid={`sandbox-row-start-crew-${inst.id}`}
          >
            {ROW_TEXT.startCrew}
          </Btn>
        </div>
      ) : null}
      {startLine ? (
        <div className="text-[12px] text-muted" data-testid={`sandbox-row-start-crew-line-${inst.id}`}>
          {startLine}
        </div>
      ) : null}
      {idleText ? (
        <div className="text-[12px] text-muted" data-testid={`sandbox-row-idle-${inst.id}`}>
          {idleText}
        </div>
      ) : null}
      {note ? (
        <div className="text-[12px] text-muted" data-testid={`sandbox-row-note-${inst.id}`}>
          {note}
        </div>
      ) : null}
      {idleSave.isError ? (
        <ErrorNotice
          variant="inline"
          message={idleSave.error instanceof Error ? idleSave.error.message : String(idleSave.error)}
        />
      ) : null}
      {!item.status && list.data?.error ? <ErrorNotice variant="inline" message={list.data.error} /> : null}
      {lastError ? <ErrorNotice variant="inline" message={lastError} testId={`sandbox-row-error-${inst.id}`} /> : null}
      {action.isError ? (
        <ErrorNotice
          variant="inline"
          message={action.error instanceof Error ? action.error.message : String(action.error)}
        />
      ) : null}
    </div>
  )
}
