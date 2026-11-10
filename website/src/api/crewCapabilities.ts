import { withDeadline } from '../lib/withDeadline'
import { agentPath } from './agentPath'
import { apiTransport } from './apiTransport'

export type CapabilitySection = 'mcpServers' | 'tools' | 'allowedTools' | 'autoApprove' | 'skills' | 'prompt' | 'model' | 'resources'
export interface CapabilityRef { section: CapabilitySection; id: string }
export interface CapabilityRow extends CapabilityRef {
  label: string
  state: 'inherited' | 'local' | 'removed'
  present: boolean
  value: unknown
  managed?: boolean
  shared_reference?: boolean
  /** On a crewmate worker row: the stored editor row a Remove drops, or `null`
   *  when the row has none of its own (it goes with its server, or stays). */
  remove?: CapabilityRef | null
}
export interface CapabilityView {
  schema_version: 1
  member: string
  // 'mate': the crewmate's own file, edited in place; it has no parent.
  mode: 'shared' | 'legacy_snapshot' | 'inherited' | 'mate'
  revision: string
  template: { name: string; source: 'builtin' | 'package' | 'custom' | 'project' | 'unknown'; scope: 'global' | 'project' | 'mate'; available: boolean; error_code?: string }
  rows: CapabilityRow[]
  connections: { id: string; label: string; managed: boolean }[]
  skills: { id: string; label: string; shared_reference: boolean }[]
  parent_changes: (CapabilityRef & { kind: 'added' | 'changed' | 'removed'; conflict: boolean; requires_approval: boolean; before: unknown; after: unknown })[]
  runtime: { status: 'pending' | 'applied' | 'failed' | 'unverified' | 'owned'; saved_revision: string; sessions: unknown[]; error_code?: string }
  /** Set when a member chat on the backend `setting` resolves to would refuse
   * this spec. A warning only: the save still lands. */
  backend_warning?: CapabilityBackendWarning | null
  /** A crewmate that owns its file: what its WORKER carries (base merged with
   * the file), as the rows Built From both lists and counts. */
  worker?: { rows: CapabilityRow[] } | null
  /** Set when Stop following would be refused for this saved setup, so the pane can say so up front. */
  detach_blocked?: 'detach_tool_limits_unkept' | 'detach_removal_unkept' | null
  /** Stopping would turn the global mcp.json merge off for the kept copy; the owner confirms it. */
  detach_mcp_json_loss?: boolean
}
export interface CapabilityBackendWarning {
  code: 'harness_unsupported' | 'tool_limits_unsupported'
  backend: string
  setting: 'agent.member_acp_backend' | 'agent.acp_backend'
  servers?: string[]
}
export type CapabilityOperation = CapabilityRef & (
  | { action: 'inherit' | 'remove' }
  | { action: 'set'; value: unknown; connection_id?: never; retain_paths?: string[] }
  | { action: 'set'; connection_id: string; value?: never; retain_paths?: never }
)
export interface CapabilityDraft {
  revision: string
  enroll: boolean
  operations: CapabilityOperation[]
  accept_parent: CapabilityRef[]
  accept_members: string[]
}
export interface CapabilityPreview extends CapabilityView {
  preview_token: string
  impact: (CapabilityRef & { member: string; change: 'added' | 'removed' | 'changed'; approval_expanded: boolean })[]
  /** Approvals this draft sets that the saved spec will not carry: a security
   * profile governs the server, the member has no enabled connection to it,
   * or Kiro Crew rebuilds that connection without approvals. */
  withheld_approvals?: (CapabilityRef & { reason: 'governed' | 'not_connected' | 'managed' | 'unexplained' })[]
}
/** Deadline for the two reads the Bot editor's Abilities pane waits on, which
 *  shows its spinner until they settle. Longer than the gateway's own 20s bound
 *  on each read (`_GET_TIMEOUT_SECS` in `dashboard/handlers/agent_capabilities.py`
 *  and `member_worker_kit.py`), so its coded 503 normally answers first; this
 *  ends the wait when no answer comes back at all. */
export const ABILITIES_READ_TIMEOUT_MS = 30_000
const { getWithSignal, post, put, j } = apiTransport
const getWithDeadline = (url: string): Promise<unknown> =>
  withDeadline(ABILITIES_READ_TIMEOUT_MS, undefined, signal => getWithSignal(url, signal).then(j))
const endpoint = (member: string) => agentPath(member, '/capabilities')
export const crewCapabilitiesApi = {
  get: (member: string): Promise<CapabilityView> => getWithDeadline(endpoint(member)) as Promise<CapabilityView>,
  preview: (member: string, draft: CapabilityDraft): Promise<CapabilityPreview> =>
    post(`${endpoint(member)}/preview`, draft).then(j) as Promise<CapabilityPreview>,
  save: (member: string, draft: CapabilityDraft & { preview_token: string }): Promise<CapabilityView> =>
    put(endpoint(member), draft).then(j) as Promise<CapabilityView>,
}
export const crewCapabilitiesKey = (member: string) => ['crew-capabilities', member] as const

/** A crewmate's worker kit: abilities added to its own file from other agents. */
export interface WorkerKitView {
  member: string
  revision: string
}
export interface WorkerKitChange {
  /** `disabledTools` / `disabledServers`: a limit on a server the worker base declares. */
  /** `hooks` / `permissions` / `toolsSettings`: run on the crewmate's worker only. */
  section: 'prompt' | 'mcpServers' | 'resources' | 'skills' | 'allowedTools' | 'excludedTools' | 'disabledTools' | 'disabledServers' | 'tools' | 'hooks' | 'permissions' | 'toolsSettings'
  id: string
  /** `added`: written by the add. `skipped`: already present, so left alone. */
  /** `replaced`: the behaviour-directions slot already holds other directions. */
  change: 'added' | 'replaced' | 'skipped'
}
export interface WorkerKitPreview {
  member: string
  template: string
  revision: string
  runs_unasked: boolean
  /** The behaviour directions the add writes; `""` when the agent has none. */
  directions?: string
  /** The directions the slot holds now, which a `replaced` row overwrites; `""` when empty. */
  current_directions?: string
  changes: WorkerKitChange[]
}
export interface WorkerKitRequest {
  template: string
  revision?: string
}
const workerKitEndpoint = (member: string) => agentPath(member, '/worker-kit')
export const crewWorkerKitApi = {
  get: (member: string): Promise<WorkerKitView> => getWithDeadline(workerKitEndpoint(member)) as Promise<WorkerKitView>,
  preview: (member: string, body: WorkerKitRequest): Promise<WorkerKitPreview> =>
    post(`${workerKitEndpoint(member)}/preview`, body).then(j) as Promise<WorkerKitPreview>,
  apply: (member: string, body: WorkerKitRequest & { revision: string }): Promise<WorkerKitView> =>
    put(workerKitEndpoint(member), body).then(j) as Promise<WorkerKitView>,
}
export const crewWorkerKitKey = (member: string) => ['crew-worker-kit', member] as const
export const capabilityKey = (row: CapabilityRef) => JSON.stringify([row.section, row.id])
