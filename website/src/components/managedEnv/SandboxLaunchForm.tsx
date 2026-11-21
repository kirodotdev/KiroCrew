/**
 * Settings → Remote Crew → "Set up a new one" → Managed environment.
 *
 * Drawn through the core's `registerRemoteProvisionerRenderer` seam for the
 * `kiro_managed_env` provisioner kind (backend: kiro_crew/managed_env). The panel owns the
 * launch progress card below this form; this form owns:
 *
 *   - one status line for Crew's own sign-in (Settings → Kiro account), from the row-lifecycle
 *     route's token-free `sign_in`: once signed in it names the account from
 *     `GET /api/managed-environments/signed-in-user`, else a link to that tab. While the core
 *     keeps the local agent off on the lane's sign-in, one more plain line says so,
 *   - one Create form: on the left the crew name and the idle timeout (typed, 15 minutes to
 *     7 days), each with a default; on the right "Before you launch": the sign-in and the
 *     network setting. Launch posts `{profile: <name>, size_key: <setting>}`: the idle timeout
 *     when it differs from the service's default. While the network setting is the restricted
 *     default, its check offers "Allow downloads…" (SandboxNetworkAsk), and Launch stays off
 *     until a choice there. An explicit Agree writes the setting BEFORE any launch,
 *   - "Sandboxes without a crew": status plus Stop / Resume / Delete for an environment with no
 *     crew row, calling `POST /api/managed-environments/{id}/{stop,resume,delete}`. An
 *     environment with a crew row gets those controls on that row under "Your crews"
 *     (SandboxCrewRowActions).
 *
 * The button rules mirror the server's: Stop only while RUNNING, Resume only while STOPPED,
 * Delete never while PENDING, no action at all on a GONE row, and nothing while an action on
 * that environment is still running. Delete asks first. An environment another client made
 * (`own === false`) is listed with its state, a line and no buttons; the server refuses every
 * action on it as well. While the List decides (`crews_from_list`), one with no record gets
 * Set up Crew (RUNNING or STOPPED) and, when STOPPED, Resume.
 */
import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle, Circle, Rocket } from 'lucide-react'
import { Btn, Card } from '../ui'
import ErrorNotice from '../ErrorNotice'
import { useConfirm } from '../ConfirmDialog'
import type { RemoteProvisionerFormProps } from '../remoteProvisionerRenderers'
import { managedEnvApi } from '../../api/managedEnvApi'
import type {
  NetcfgStepResult,
  SandboxAction,
  SandboxItem,
  SandboxList,
  SandboxRowLifecycle,
  SandboxSetUpAnswer,
  SandboxSignedInUser,
} from '../../api/managedEnvApi'
import { KIRO_SIGN_IN_PATH } from '../../pages/developer/kiroSignInLink'
import SandboxNetworkCard, {
  SANDBOX_NETCFG_QUERY_KEY,
  SandboxNetworkAsk,
  sandboxNetcfgNeedsAsk,
  sandboxNetcfgSeen,
} from './SandboxNetworkCard'
import SimpleSelect from '../SimpleSelect'
import { IDLE_MAX, IDLE_MIN, IDLE_UNITS, idleMinutes } from './SandboxIdleTimeoutDialog'
import type { IdleUnit } from './SandboxIdleTimeoutDialog'
import {
  CREATE_TEXT,
  LIST_TEXT,
  SIGN_IN_TEXT,
  deleteTitle,
  idleUnitLabel,
  signedInAsEmail,
  signedInAsTail,
  withCrewPointer,
} from './text'

export const SANDBOX_PROVISIONER_KIND = 'kiro_managed_env'
export const SANDBOX_LIST_QUERY_KEY = ['managed-env-list'] as const

/** What a box with no record (not this home's, no Crew marker) offers while the List decides
 * (`crews_from_list`): Set up Crew when RUNNING or STOPPED, and Resume when STOPPED; nothing while
 * an action on it runs. Never Stop, Delete, Idle timeout or Start Crew. */
export function sandboxSetUpOffered(item: SandboxItem, fromList: boolean): { setUp: boolean; resume: boolean } {
  const none = { setUp: false, resume: false }
  if (!fromList || item.own !== false || item.crew_marker !== false || item.action?.running) return none
  return { setUp: item.status === 'RUNNING' || item.status === 'STOPPED', resume: item.status === 'STOPPED' }
}

/** The core's crews list (RemoteCrewPanel's `['instances']` query; its refetch is off). */
export const CREWS_QUERY_KEY = ['instances'] as const

/** An answer that wrote Remote Crew rows from the List (`rows_added`) re-reads the core's crews
 * list once, so the crews tab shows the new rows without opening "Set up a new one". */
export function useCrewsReadAfterRowsAdded(list: SandboxList | undefined, at: number) {
  const qc = useQueryClient()
  const added = list?.rows_added === true
  useEffect(() => {
    if (added) void qc.invalidateQueries({ queryKey: CREWS_QUERY_KEY })
  }, [added, at, qc])
}

/** A box made by another of this user's Crew dashboards: not this home's, with the Crew marker. */
export function sandboxFromAnotherDashboard(item: SandboxItem): boolean {
  return item.own === false && item.crew_marker === true
}

/** `GET /api/managed-environments/row-lifecycle`: the lane switch and Crew's own sign-in,
 * token-free, with no service call. One cache entry for the form and every row, never polled and
 * not re-read on focus. A `fresh` reader (the form, a row's detail block) re-reads it when it
 * mounts, so a sign-in made under Settings → Kiro account shows on the next visit. */
export const SANDBOX_ROW_LIFECYCLE_QUERY_KEY = ['managed-env-row-lifecycle'] as const

export function useSandboxRouteState(fresh = false) {
  return useQuery({
    queryKey: SANDBOX_ROW_LIFECYCLE_QUERY_KEY,
    queryFn: () => managedEnvApi.sandboxRowLifecycle(),
    refetchInterval: false,
    refetchOnWindowFocus: false,
    ...(fresh ? { staleTime: 0 } : {}),
  })
}

/** Signed in only on the route's `signed_in`: a missing field, a failed read or a 404 is not. */
export function sandboxSignedIn(answer: SandboxRowLifecycle | undefined): boolean {
  return answer?.sign_in?.state === 'signed_in'
}

export const SANDBOX_SIGNED_IN_USER_QUERY_KEY = ['managed-env-signed-in-user'] as const
/** Re-read while signed in, so a switch from one account to another shows the new name. The
 * server answers from its cache: still one GetUserInfo per sign-in. */
export const SANDBOX_SIGNED_IN_USER_REFRESH_MS = 30_000

/** Who the lane is signed in as, read only while signed in. */
export function useSandboxSignedInUser(signedIn: boolean) {
  return useQuery({
    queryKey: SANDBOX_SIGNED_IN_USER_QUERY_KEY,
    queryFn: () => managedEnvApi.sandboxSignedInUser(),
    enabled: signedIn,
    refetchInterval: signedIn ? SANDBOX_SIGNED_IN_USER_REFRESH_MS : false,
    refetchOnWindowFocus: signedIn,
    staleTime: 0,
  })
}

/** The signed-in line, by the name the server could read. */
export function sandboxSignedInLine(user: SandboxSignedInUser | undefined): string {
  if (user?.email) return signedInAsEmail(user.email)
  if (user?.user_id_tail) return signedInAsTail(user.user_id_tail)
  if (user?.state === 'unknown') return SIGN_IN_TEXT.nameUnknown
  return SIGN_IN_TEXT.signedIn
}

/** The status line (the signed-in account by name, else the route's own message), and the
 * local agent's line. */
export function SandboxSignInStatus({ answer, failed }: { answer: SandboxRowLifecycle | undefined; failed: boolean }) {
  const signedIn = sandboxSignedIn(answer)
  const user = useSandboxSignedInUser(signedIn)
  if (!answer && !failed) return null
  const signIn = answer?.sign_in
  const line = signedIn ? sandboxSignedInLine(user.data) : signIn?.message || SIGN_IN_TEXT.required
  return (
    <div className="space-y-1 text-[12px] text-muted">
      <div data-testid="sandbox-signin-line">
        {signedIn ? line : <a href={KIRO_SIGN_IN_PATH} className="underline">{line}</a>}
      </div>
      {signIn?.local_agent === 'off' ? (
        <div data-testid="sandbox-local-agent-off">{SIGN_IN_TEXT.localAgentOff}</div>
      ) : null}
    </div>
  )
}

/** True only when the list answer turns the row lifecycle on (`row_lifecycle: true`). */
export function sandboxRowLifecycle(list: SandboxList | undefined): boolean {
  return list?.row_lifecycle === true
}

/** The form's list: every environment, or, with the row lifecycle on, only those with no crew row. */
export function sandboxFormItems(list: SandboxList | undefined): SandboxItem[] {
  const all = list?.sandboxes ?? []
  return sandboxRowLifecycle(list) ? all.filter(i => !i.instance_id) : all
}

/** The service's idle timeout when none is sent. */
export const SANDBOX_IDLE_DEFAULT = 60
/** What Launch sends as the provisioner's own size_key (engine.py create_options): the idle
 * timeout when it differs from the service's default. Never while idle stop is off. */
export function sandboxCreateOptions(idle: number, idleOff: boolean): string {
  // the wire form `idle=<minutes>`, built without a literal: it is a value, never shown
  return !idleOff && idle !== SANDBOX_IDLE_DEFAULT ? new URLSearchParams({ idle: String(idle) }).toString() : ''
}

/** Same rule as engine.py's crew-name check. */
const CREW_RE = /^[a-z][a-z0-9-]{0,30}[a-z0-9]$/

export function sandboxNameProblem(name: string): string {
  if (!name) return CREATE_TEXT.nameMissing
  if (!CREW_RE.test(name) || name.includes('--')) return CREATE_TEXT.nameRule
  return ''
}

/** Which actions a row offers, by status. An action already running offers none, and so does an
 * environment another client made (`own === false`). */
export function sandboxActionsFor(item: SandboxItem): Record<SandboxAction, boolean> {
  if (item.own === false) return { stop: false, resume: false, delete: false }
  const busy = Boolean(item.action?.running)
  const status = item.status
  return {
    stop: !busy && status === 'RUNNING',
    resume: !busy && status === 'STOPPED',
    // never on a box outside this home's ledger (deletable false)
    delete: !busy && status !== 'PENDING' && status !== 'GONE' && item.deletable !== false,
  }
}

function message(err: unknown): string {
  return err instanceof Error ? err.message : String(err)
}

/** Poll quickly while something is moving, slowly otherwise. */
export function sandboxPollMs(items: SandboxItem[] | undefined): number {
  const moving = (items ?? []).some(
    i =>
      i.status === 'PENDING' ||
      i.action?.running ||
      (i.status !== 'RUNNING' && i.status !== 'STOPPED' && i.status !== 'GONE' && i.status !== ''),
  )
  return moving ? 3_000 : 30_000
}

/** True while an environment whose Delete was accepted is still listed and not GONE. */
export function sandboxStillDeleting(items: SandboxItem[] | undefined, deleting: string[]): boolean {
  return (items ?? []).some(i => deleting.includes(i.sandbox_id) && i.status !== 'GONE')
}

function SandboxRow({
  item,
  onAction,
  working,
  byMarker,
  fromList = false,
  onSetUp,
  setUpLine = '',
}: {
  item: SandboxItem
  onAction: (item: SandboxItem, action: SandboxAction) => void
  working: boolean
  byMarker: boolean
  fromList?: boolean
  onSetUp?: (item: SandboxItem) => void
  setUpLine?: string
}) {
  const can = sandboxActionsFor(item)
  const offer = sandboxSetUpOffered(item, fromList)
  const line = setUpLine || (fromList ? item.setup_line ?? '' : '')
  const label = item.name || item.display_name || item.sandbox_id
  return (
    <li className="flex flex-wrap items-center justify-between gap-2 py-2" data-testid={`sandbox-row-${item.sandbox_id}`}>
      <div className="min-w-0">
        <span className="font-mono text-[13px]">{label}</span>{' '}
        <span className="text-[12px] text-muted" data-testid={`sandbox-status-${item.sandbox_id}`}>
          {item.status || LIST_TEXT.unknownStatus}
        </span>
        {item.action?.running ? <span className="ml-2 text-[12px] text-muted">{LIST_TEXT.inProgress}</span> : null}
        {item.action && !item.action.running && item.action.error ? (
          <div className="text-[12px] text-danger" data-testid={`sandbox-action-error-${item.sandbox_id}`}>
            {item.action.error}
          </div>
        ) : null}
        {item.own !== false ? null : !byMarker ? (
          <div className="text-[12px] text-muted" data-testid={`sandbox-other-client-${item.sandbox_id}`}>
            {LIST_TEXT.otherClient}
          </div>
        ) : sandboxFromAnotherDashboard(item) ? (
          <div className="text-[12px] text-muted" data-testid={`sandbox-marker-${item.sandbox_id}`}>
            {LIST_TEXT.markerLine}
          </div>
        ) : (
          <div className="text-[12px] text-muted" data-testid={`sandbox-not-set-up-${item.sandbox_id}`}>
            {LIST_TEXT.notSetUp}
          </div>
        )}
        {line ? (
          <div className="text-[12px] text-muted" data-testid={`sandbox-setup-line-${item.sandbox_id}`}>
            {line}
          </div>
        ) : null}
      </div>
      {offer.setUp ? (
        <div className="flex gap-2" data-testid={`sandbox-setup-buttons-${item.sandbox_id}`}>
          {offer.resume ? (
            <Btn onClick={() => onAction(item, 'resume')} disabled={working} data-testid={`sandbox-resume-${item.sandbox_id}`}>
              {LIST_TEXT.resume}
            </Btn>
          ) : null}
          <Btn onClick={() => onSetUp?.(item)} disabled={working} data-testid={`sandbox-set-up-${item.sandbox_id}`}>
            {LIST_TEXT.setUpCrew}
          </Btn>
        </div>
      ) : null}
      {item.status === 'GONE' || item.own === false ? null : (
        <div className="flex gap-2">
          <Btn
            onClick={() => onAction(item, 'stop')}
            disabled={!can.stop || working}
            data-testid={`sandbox-stop-${item.sandbox_id}`}
          >
            {LIST_TEXT.stop}
          </Btn>
          <Btn
            onClick={() => onAction(item, 'resume')}
            disabled={!can.resume || working}
            data-testid={`sandbox-resume-${item.sandbox_id}`}
          >
            {LIST_TEXT.resume}
          </Btn>
          {item.deletable === false ? null : (
            <Btn
              onClick={() => onAction(item, 'delete')}
              disabled={!can.delete || working}
              data-testid={`sandbox-delete-${item.sandbox_id}`}
            >
              {LIST_TEXT.delete}
            </Btn>
          )}
        </div>
      )}
    </li>
  )
}

export default function SandboxLaunchForm({ launch, launching, disabled, activeJob }: RemoteProvisionerFormProps) {
  const qc = useQueryClient()
  const { confirm, confirmDialog } = useConfirm()
  const [name, setName] = useState('sandbox')
  const [touched, setTouched] = useState(false)
  // the Create form's settings, each with its default (one click still launches)
  const [idleText, setIdleText] = useState(String(SANDBOX_IDLE_DEFAULT))
  const [idleUnit, setIdleUnit] = useState<IdleUnit>('minutes')
  // the network check's choice: Without lets Launch go ahead with the setting as it is
  const [netWithout, setNetWithout] = useState(false)

  const jobRunning = Boolean(activeJob && !['done', 'failed', 'cancelled'].includes(activeJob.status))
  const launchBusy = launching || jobRunning

  // Crew's own sign-in, from the route every row reads too: one request, one cache entry.
  const route = useSandboxRouteState(true)
  // Environments whose Delete was accepted (a 202: it runs on). The list is re-read until each
  // is absent or GONE, not just once while the delete is still running.
  const [deleting, setDeleting] = useState<string[]>([])
  const list = useQuery({
    queryKey: SANDBOX_LIST_QUERY_KEY,
    queryFn: () => managedEnvApi.sandboxList(),
    // A launch in flight creates an environment the list has not seen yet: poll fast for it.
    refetchInterval: q =>
      launchBusy || sandboxStillDeleting(q.state.data?.sandboxes, deleting)
        ? 3_000
        : sandboxPollMs(q.state.data?.sandboxes),
  })
  useCrewsReadAfterRowsAdded(list.data, list.dataUpdatedAt)
  const action = useMutation({
    mutationFn: ({ item, act }: { item: SandboxItem; act: SandboxAction }) =>
      managedEnvApi.sandboxAction(item.sandbox_id, act),
    onSuccess: (_res, { item, act }) => {
      if (act === 'delete') setDeleting(ids => (ids.includes(item.sandbox_id) ? ids : [...ids, item.sandbox_id]))
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY }),
  })
  // Set up Crew's answer, by environment: its line; found also re-reads the crews list
  const fromList = list.data?.crews_from_list === true
  const [setUpLines, setSetUpLines] = useState<Record<string, string>>({})
  const [foundLine, setFoundLine] = useState('')
  const setUp = useMutation({
    mutationFn: (item: SandboxItem) => managedEnvApi.sandboxSetUpCrew(item.sandbox_id),
    onSuccess: (res: SandboxSetUpAnswer, item) => {
      const text = res.line ?? ''
      if (res.result === 'found') {
        setFoundLine(text)
        void qc.invalidateQueries({ queryKey: CREWS_QUERY_KEY })
      } else if (text) {
        setSetUpLines(m => ({ ...m, [item.sandbox_id]: text }))
      }
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY }),
  })

  // The list must refresh the moment sign-in turns signed_in. Only a change from another known
  // state counts, so a page that loads already signed in does not read the list twice.
  const signinState = route.data ? (sandboxSignedIn(route.data) ? 'signed_in' : 'signin_required') : undefined
  const prevSignin = useRef<string | undefined>(undefined)
  useEffect(() => {
    if (signinState === 'signed_in' && prevSignin.current !== undefined && prevSignin.current !== 'signed_in') {
      void qc.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
    }
    prevSignin.current = signinState
  }, [signinState, qc])

  // The list must show the new environment, and its end state, as soon as a launch moves.
  const jobKey = `${launching}:${activeJob?.id ?? ''}:${activeJob?.status ?? ''}`
  useEffect(() => {
    void qc.invalidateQueries({ queryKey: SANDBOX_LIST_QUERY_KEY })
  }, [jobKey, qc])

  // The network setting: read once signed in; Create asks first while it is the restricted default.
  const netcfg = useQuery({
    queryKey: SANDBOX_NETCFG_QUERY_KEY,
    queryFn: () => managedEnvApi.netcfgStepStatus(),
    enabled: signinState === 'signed_in',
    refetchInterval: false,
  })
  const [askOpen, setAskOpen] = useState(false)
  const [netBusy, setNetBusy] = useState(false)
  const [netResult, setNetResult] = useState<NetcfgStepResult | null>(null)
  const runNet = async (call: () => Promise<NetcfgStepResult>): Promise<NetcfgStepResult> => {
    setNetBusy(true)
    try {
      const res = await call()
      setNetResult(res)
      return res
    } catch (err) {
      const res = { code: 'error', ok: false, message: message(err) }
      setNetResult(res)
      return res
    } finally {
      setNetBusy(false)
      void qc.invalidateQueries({ queryKey: SANDBOX_NETCFG_QUERY_KEY })
    }
  }
  const onAgree = async () => {
    const s = netcfg.data
    if (!s) return
    setAskOpen(false)
    // Agree writes the setting and nothing more; Launch, after it, creates.
    await runNet(() => managedEnvApi.netcfgStepApply(sandboxNetcfgSeen(s)))
  }
  const onWithout = () => {
    setAskOpen(false)
    setNetWithout(true)
  }
  const onRestore = () => void runNet(() => managedEnvApi.netcfgStepRestore())

  const rowLifecycle = sandboxRowLifecycle(list.data)
  const items = sandboxFormItems(list.data)
  // With the row lifecycle on, the environments this list leaves out are managed on their crew rows.
  const withCrew = rowLifecycle ? (list.data?.sandboxes ?? []).length - items.length : 0
  const nameProblem = sandboxNameProblem(name)
  const signedIn = sandboxSignedIn(route.data)
  const idleOff = list.data?.idle_off === true
  const idle = idleMinutes(idleText, idleUnit)
  const idleOk = idleOff || (idle !== null && idle >= IDLE_MIN && idle <= IDLE_MAX)
  // the last launch met idle stop off (its plain line is the job's error): say so here too
  const idleOffRelaunch =
    idleOff && activeJob?.status === 'failed' && (activeJob.error ?? '').includes(CREATE_TEXT.idleOffRelaunch)
  // nothing is asked after Launch: a blocked network is decided in the check first
  const netAsks = sandboxNetcfgNeedsAsk(netcfg.data)
  const netReady = signedIn && !netcfg.isLoading && (!netAsks || netWithout)
  const canCreate = signedIn && !nameProblem && idleOk && netReady && !launching && !disabled && !jobRunning
  const onLaunch = () => {
    setTouched(true)
    if (!canCreate) return
    const options = sandboxCreateOptions(idle ?? SANDBOX_IDLE_DEFAULT, idleOff)
    launch({ profile: name, size_key: options })
  }

  const onAction = async (item: SandboxItem, act: SandboxAction) => {
    if (act === 'delete') {
      const ok = await confirm({
        title: deleteTitle(item.name || item.display_name || item.sandbox_id),
        body: <p>{LIST_TEXT.deleteBody}</p>,
        confirmLabel: LIST_TEXT.deleteConfirm,
        danger: true,
      })
      if (!ok) return
    }
    action.mutate({ item, act })
  }

  return (
    <div className="space-y-4" data-testid="sandbox-launch-form">
      {confirmDialog}
      {askOpen && netcfg.data ? (
        <SandboxNetworkAsk
          status={netcfg.data}
          busy={netBusy}
          onAgree={() => void onAgree()}
          onWithout={onWithout}
          onCancel={() => setAskOpen(false)}
        />
      ) : null}
      <Card className="p-0" data-testid="sandbox-create-form">
        <div className="flex flex-wrap items-start gap-3 border-b border-border p-4">
          <div className="min-w-0 flex-1">
            <h3 className="text-[15px] font-semibold text-text-strong">{CREATE_TEXT.title}</h3>
            <p className="text-[12px] text-muted">{CREATE_TEXT.lead}</p>
          </div>
          <Btn primary onClick={onLaunch} disabled={!canCreate || netBusy} data-testid="sandbox-create">
            <Rocket size={14} aria-hidden="true" /> {launching ? CREATE_TEXT.launching : CREATE_TEXT.launch}
          </Btn>
        </div>
        <div className="grid gap-6 p-4 md:grid-cols-2">
          <div className="space-y-4">
            <label className="block">
              <span className="text-[12px] font-medium text-muted">{CREATE_TEXT.name}</span>
              <input
                value={name}
                onChange={e => {
                  setTouched(true)
                  setName(e.target.value.trim().toLowerCase())
                }}
                spellCheck={false}
                aria-invalid={touched && Boolean(nameProblem)}
                className="mt-1 w-full font-mono"
                aria-label={CREATE_TEXT.name}
                data-testid="sandbox-name"
              />
              {touched && nameProblem ? (
                <span className="mt-1 block text-[12px] text-danger">{nameProblem}</span>
              ) : (
                <span className="mt-1 block text-[12px] text-muted">{CREATE_TEXT.nameHint}</span>
              )}
            </label>
            <div>
              <span className="text-[12px] font-medium text-muted">{CREATE_TEXT.idle}</span>
              <div className="mt-1 flex gap-2">
                <input
                  value={idleText}
                  onChange={e => setIdleText(e.target.value)}
                  inputMode="numeric"
                  disabled={idleOff}
                  aria-label={CREATE_TEXT.idle}
                  aria-invalid={!idleOk}
                  className="w-24 font-mono"
                  data-testid="sandbox-create-idle"
                />
                <span data-testid="sandbox-create-idle-unit">
                  <SimpleSelect
                    options={IDLE_UNITS}
                    optionLabels={IDLE_UNITS.map(u => idleUnitLabel(u))}
                    value={idleUnit}
                    onChange={next => setIdleUnit(next as IdleUnit)}
                    disabled={idleOff}
                    aria-label={CREATE_TEXT.idleUnit}
                  />
                </span>
              </div>
              <span
                className={`mt-1 block text-[12px] ${idleOk ? 'text-muted' : 'text-danger'}`}
                data-testid="sandbox-create-idle-line"
              >
                {idleOffRelaunch
                  ? CREATE_TEXT.idleOffRelaunch
                  : idleOff
                    ? CREATE_TEXT.idleOff
                    : idleOk
                      ? CREATE_TEXT.idleHint
                      : CREATE_TEXT.idleBad}
              </span>
            </div>
          </div>
          <div className="space-y-3" data-testid="sandbox-create-checks">
            <div className="text-[12px] font-medium text-muted">{CREATE_TEXT.checks}</div>
            <div className="flex items-start gap-2" data-testid="sandbox-check-signin">
              {signedIn ? (
                <CheckCircle size={16} className="text-ok" aria-hidden="true" />
              ) : (
                <AlertTriangle size={16} className="text-warn" aria-hidden="true" />
              )}
              <SandboxSignInStatus answer={route.data} failed={route.isError} />
            </div>
            <div className="flex items-start gap-2" data-testid="sandbox-check-network">
              {!signedIn ? (
                <Circle size={16} className="text-muted" aria-hidden="true" />
              ) : netAsks && !netWithout ? (
                <AlertTriangle size={16} className="text-warn" aria-hidden="true" />
              ) : (
                <CheckCircle size={16} className="text-ok" aria-hidden="true" />
              )}
              <div className="space-y-2 text-[12px]">
                <div data-testid="sandbox-check-network-line">
                  {!signedIn
                    ? CREATE_TEXT.netPending
                    : netAsks
                      ? netWithout
                        ? CREATE_TEXT.netWithout
                        : CREATE_TEXT.netAsk
                      : CREATE_TEXT.netOk}
                </div>
                {signedIn && netAsks && !netWithout ? (
                  <Btn onClick={() => setAskOpen(true)} disabled={netBusy} data-testid="sandbox-net-allow">
                    {CREATE_TEXT.netAllow}
                  </Btn>
                ) : null}
                {signedIn ? (
                  <SandboxNetworkCard status={netcfg.data} result={netResult} busy={netBusy} onRestore={onRestore} />
                ) : null}
              </div>
            </div>
          </div>
        </div>
      </Card>
      <Card className="p-4">
        <h3 className="text-[15px] font-semibold text-text-strong">
          {rowLifecycle ? LIST_TEXT.noCrewTitle : LIST_TEXT.yours}
        </h3>
        {list.isError ? <ErrorNotice message={message(list.error)} /> : null}
        {list.data?.error ? <ErrorNotice message={list.data.error} /> : null}
        {action.isError ? <ErrorNotice message={message(action.error)} /> : null}
        {setUp.isError ? <ErrorNotice message={message(setUp.error)} /> : null}
        {foundLine ? (
          <p className="mt-2 text-[12px] text-muted" data-testid="sandbox-setup-found">
            {foundLine}
          </p>
        ) : null}
        {list.isSuccess && items.length === 0 && rowLifecycle && (list.data?.sandboxes ?? []).length > 0 ? (
          <p className="mt-2 text-[12px] text-muted" data-testid="sandbox-all-have-crews">
            {LIST_TEXT.allHaveCrews}
          </p>
        ) : list.isSuccess && items.length === 0 ? (
          <p className="mt-2 text-[12px] text-muted" data-testid="sandbox-empty">
            {LIST_TEXT.empty}
          </p>
        ) : (
          <>
            {list.isSuccess && withCrew > 0 ? (
              <p className="mt-2 text-[12px] text-muted" data-testid="sandbox-with-crew-pointer">
                {withCrewPointer(withCrew)}
              </p>
            ) : null}
            {[false, true].map(group => {
              // the marker group goes after the others
              const rows = items.filter(i => (rowLifecycle && sandboxFromAnotherDashboard(i)) === group)
              return rows.length ? (
                <ul
                  key={String(group)}
                  className="mt-2 divide-y divide-border"
                  data-testid={group ? 'sandbox-marker-group' : undefined}
                >
                  {rows.map(i => (
                    <SandboxRow
                      key={i.sandbox_id}
                      item={i}
                      onAction={onAction}
                      working={action.isPending || setUp.isPending}
                      byMarker={rowLifecycle}
                      fromList={fromList}
                      onSetUp={item => setUp.mutate(item)}
                      setUpLine={setUpLines[i.sandbox_id] ?? ''}
                    />
                  ))}
                </ul>
              ) : null
            })}
          </>
        )}
      </Card>
    </div>
  )
}
