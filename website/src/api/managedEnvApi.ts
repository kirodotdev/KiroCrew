/**
 * The managed-environment lane's routes (`kiro_crew/managed_env/routes.py`), built on the
 * blessed `apiTransport`. Every route runs the owner check and answers 404 while the
 * `crew_client` feature preview is off. No answer carries a bearer.
 */
import { apiTransport } from './apiTransport'

const BASE = '/api/managed-environments'

/** The `sign_in` field of GET row-lifecycle. `signed_in` only for a usable Identity Center
 * sign-in on the lane's start URL; anything else is `signin_required`. `local_agent` is `off`
 * while the core's registered-sign-in fence keeps the local agent off. */
export interface SandboxSignIn {
  state: 'signed_in' | 'signin_required'
  message: string
  local_agent: 'on' | 'off'
}

/** GET row-lifecycle: the lane switch and the sign-in, with no service call. */
export interface SandboxRowLifecycle {
  row_lifecycle: boolean
  /** Always sent by the route; read as `signin_required` when absent. */
  sign_in?: SandboxSignIn
}

/** GET signed-in-user: who the lane is signed in as (one GetUserInfo per sign-in, cached
 * server-side). One of the three name shapes, or `signed_out`. */
export interface SandboxSignedInUser {
  email?: string
  user_id_tail?: string
  state?: 'unknown' | 'signed_out'
}

/** GET {id}/connect-phase: how far that environment's connect has got, from the dashboard's
 * memory, with no service call. `''` is no connect under way. */
export interface SandboxConnectPhase {
  phase: '' | 'starting' | 'tunnel' | 'signing_in'
}

/** The three row actions: POST {id}/{action}. */
export type SandboxAction = 'stop' | 'resume' | 'delete'

/** One row of GET list. */
export interface SandboxItem {
  /** The execEnvironmentId; what the action routes take. */
  sandbox_id: string
  /** Upper-case: PENDING, RUNNING, STOPPED, ..., or GONE (listed row, environment absent). */
  status: string
  display_name: string
  created_at: number
  instance_id: string
  name: string
  ssh_host: string
  /** The last or running action on this environment, if any. `note`: a line for an action that
   * ended without an error but isn't over (an idle-timeout restart that hasn't begun). */
  action: { action: string; running: boolean; error: string; at: number; note?: string } | null
  /** False for an environment another client made (Kiro web, the IDE, another Crew). */
  own?: boolean
  /** True when the displayName is a Crew launch's own `kc-<6 hex>` tag. A listing hint only. */
  crew_marker?: boolean
  /** False for a box that is not in this home's ledger (no Delete). */
  deletable?: boolean
  /** The service's `idleTimeoutMinutes`: where idle stop is on, the minutes before it stops. */
  idle_timeout_minutes?: number | null
  /** On a box with no record, the last line of Set up Crew's one read in its RUNNING period. */
  setup_line?: string
  /** This box's connect was refused by the organization's policy; the one plain line to show. */
  policy_line?: string
}

/** POST {id}/rename: `everywhere` (every dashboard of the user shows the name), `unchanged`,
 * or `no` with `line` saying why. A non-empty `line` is a failure, shown in the dialog. */
export interface SandboxRenameAnswer {
  renamed: 'everywhere' | 'unchanged' | 'no'
  name?: string
  line: string
}

/** POST {id}/set-up-crew: the one read's result and line on a RUNNING box, or
 * `{ ok, action: 'resume' }` (202) on a STOPPED one, whose read runs at RUNNING. */
export interface SandboxSetUpAnswer {
  result?: 'found' | 'not_found' | 'failed'
  line?: string
  ok?: boolean
  action?: string
}

/** GET network-step/status: the user's network setting. */
export interface NetcfgStepStatus {
  /** `status` on success; otherwise a result code such as `read_failed` with `message`. */
  code: string
  ok: boolean
  message?: string
  networkAccess?: string
  customDomains?: string[]
  needsAgreement?: boolean
  canRestore?: boolean
  dialog?: string
  appliesNote?: string
}

/** The answer of apply / restore: a result code, one plain message, and the settings shown when two disagree. */
export interface NetcfgStepResult {
  code: string
  ok: boolean
  message: string
  shown?: Record<string, unknown>
}

export interface SandboxList {
  sandboxes: SandboxItem[]
  error: string
  /** True puts Stop / Resume / Delete on each crew's row under "Your crews" and cuts the form's
   * list to environments with no crew row. */
  row_lifecycle?: boolean
  /** True for 10 minutes after the idle switch's 400 on an idle-timeout change: the control is greyed. */
  idle_off?: boolean
  /** True when this answer wrote Remote Crew rows from the List (the crews list is re-read). */
  rows_added?: boolean
  /** True while the user's List decided this answer. Start Crew is drawn only under it. */
  crews_from_list?: boolean
  /** True once the guard has stopped the client for this run (`error` says so plainly). */
  client_stopped?: boolean
}

const id = (sandboxId: string) => `${BASE}/${encodeURIComponent(sandboxId)}`

export const managedEnvApi = {
  netcfgStepStatus: async (): Promise<NetcfgStepStatus> =>
    apiTransport.j(await apiTransport.get(`${BASE}/network-step/status`)) as Promise<NetcfgStepStatus>,
  /** `agree` is sent as the literal true only from the Agree button; `seen` is the setting the person was shown. */
  netcfgStepApply: async (seen: { networkAccess: string; customDomains: string[] }): Promise<NetcfgStepResult> =>
    apiTransport.j(
      await apiTransport.post(`${BASE}/network-step/apply`, { agree: true, seen }),
    ) as Promise<NetcfgStepResult>,
  netcfgStepRestore: async (): Promise<NetcfgStepResult> =>
    apiTransport.j(await apiTransport.post(`${BASE}/network-step/restore`, {})) as Promise<NetcfgStepResult>,
  sandboxRowLifecycle: async (): Promise<SandboxRowLifecycle> =>
    apiTransport.j(await apiTransport.get(`${BASE}/row-lifecycle`)) as Promise<SandboxRowLifecycle>,
  sandboxSignedInUser: async (): Promise<SandboxSignedInUser> =>
    apiTransport.j(await apiTransport.get(`${BASE}/signed-in-user`)) as Promise<SandboxSignedInUser>,
  sandboxList: async (): Promise<SandboxList> =>
    apiTransport.j(await apiTransport.get(`${BASE}/list`)) as Promise<SandboxList>,
  sandboxConnectPhase: async (sandboxId: string): Promise<SandboxConnectPhase> =>
    apiTransport.j(await apiTransport.get(`${id(sandboxId)}/connect-phase`)) as Promise<SandboxConnectPhase>,
  sandboxAction: async (sandboxId: string, action: SandboxAction): Promise<{ ok: boolean; action: string }> =>
    apiTransport.j(await apiTransport.post(`${id(sandboxId)}/${action}`, {})) as Promise<{
      ok: boolean
      action: string
    }>,
  /** One run of the box's boot hook (no retry); the page makes the one Connect after a start. */
  sandboxStartCrew: async (sandboxId: string): Promise<{ started: boolean; line: string }> =>
    apiTransport.j(await apiTransport.post(`${id(sandboxId)}/start-crew`, {})) as Promise<{
      started: boolean
      line: string
    }>,
  sandboxSetUpCrew: async (sandboxId: string): Promise<SandboxSetUpAnswer> =>
    apiTransport.j(await apiTransport.post(`${id(sandboxId)}/set-up-crew`, {})) as Promise<SandboxSetUpAnswer>,
  sandboxRename: async (sandboxId: string, name: string): Promise<SandboxRenameAnswer> =>
    apiTransport.j(await apiTransport.post(`${id(sandboxId)}/rename`, { name })) as Promise<SandboxRenameAnswer>,
  /** One UpdateSandbox of the idle timeout; the list then reports the action. */
  sandboxIdleTimeout: async (sandboxId: string, minutes: number): Promise<{ ok: boolean; action: string }> =>
    apiTransport.j(await apiTransport.post(`${id(sandboxId)}/idle-timeout`, { minutes })) as Promise<{
      ok: boolean
      action: string
    }>,
}
