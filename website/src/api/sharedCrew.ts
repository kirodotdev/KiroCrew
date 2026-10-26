/**
 * Shared Crew wire contracts. Mirrors `kiro_crew.shared_crew` (contracts.py,
 * admission.py, collab.py); the owning spec is
 * docs/system-specs/modules/shared-crew.md. Types only plus the state-machine
 * table: no request methods live here, so adding a Shared Crew endpoint still
 * goes through its domain's `create*Endpoints` factory.
 *
 * Every principal the browser holds is server-issued. The client never sends a
 * principal as "who I am"; the server stamps the authenticated actor.
 */

export const SHARED_CREW_SCHEMA_VERSION = 1 as const

export type PrincipalKind = 'human' | 'machine' | 'system' | 'unknown'

export const UNKNOWN_PRINCIPAL_ID = 'p_unknown'

export interface DisplaySnapshot {
  name: string
  login: string
  captured_at: number | null
}

/** Browser-facing principal (`Principal.to_public_dict`). */
export interface PublicPrincipal {
  id: string
  kind: PrincipalKind
  display: DisplaySnapshot
  /** The channel a channel sender was seen through (`'slack'`): attribution
   *  only, it grants nothing. Absent for a direct (dashboard) principal. */
  via?: string
}

export type AttributionAvailability = 'recorded' | 'legacy_missing' | 'unattributed'

export type SessionTriggerKind = 'scheduled' | 'webhook' | 'system'

/** What started a session no person started (`shared_crew.trigger`). A label,
 *  never a person: `configured_by` is a verified principal only when recorded. */
export interface SessionTrigger {
  kind: SessionTriggerKind
  ref: string
  name: string
  configured_by: PublicPrincipal | null
}

export interface SessionAttribution {
  creator: PublicPrincipal
  availability: AttributionAvailability
  created_at: number | null
  trigger?: SessionTrigger
}

/** `unattributed` lists sessions with no human creator and no named trigger;
 *  `unknown` is its older spelling, which the server still accepts. */
export type CreatorFilter = 'all' | 'mine' | 'unattributed' | 'unknown' | (string & { readonly __principalId?: never })

export interface SessionPage<T> {
  schema_version: number
  items: T[]
  total_matching: number
  hidden_by_filter: number
  next_cursor: string
}

export type ActionType =
  | 'session_create'
  | 'message'
  | 'approval'
  | 'question_answer'
  | 'steer'
  | 'cancel'
  | 'retry'
  | 'control'
  | 'memory_change'

export type ActionOutcome = 'accepted' | 'applied' | 'rejected' | 'conflict' | 'failed'

export type CapabilityState = 'available' | 'degraded' | 'disabled' | 'unavailable'
export type EnforcementLevel = 'hard' | 'best_effort' | 'none'
export type MembershipLink = '' | 'tailscale_admin' | 'console'

/** `host_power.reported()`: the capabilities' `keep_awake` row. */
export interface KeepAwakeCapability {
  state: 'available' | 'unavailable'
  reason: string
  backend: string
  laptop: boolean
  laptop_reason: string
}

export interface SharedCrewCapabilities {
  schema_version: number
  attribution: CapabilityState
  presence: CapabilityState
  queueing: CapabilityState
  containment: CapabilityState
  containment_level: EnforcementLevel
  authoritative_directory: CapabilityState
  membership_link: MembershipLink
  remote_actions: CapabilityState
  protocols: number[]
  /** Why a capability field is not what its sources would answer: `no_source`
   *  (nothing registered for it) or `source_failed` (its source raised). Only
   *  fields with such a reason appear. */
  reasons: Partial<Record<CapabilityField, CapabilityReason>>
  /** Each enterprise seam's state; the public build answers `not_configured`. */
  enterprise: Record<EnterpriseCapabilityKey, EnterpriseCapability>
  /** Each shared-access setup path's availability. An older gateway omits it. */
  setup_paths?: Record<SetupPath, SetupPathCapability>
  /** The three access modes, each with its own requirement. */
  access_modes?: AccessModes
  /** The hosted platforms workload identity supports, and this host. */
  workload_identity?: WorkloadIdentityHost
  /** The direct tailnet listener. An older gateway omits it. */
  tailnet_direct?: TailnetDirect
  /** Which verified path names people: `tailscale_direct`, or empty. */
  attribution_via?: '' | 'tailscale_direct'
  /** Whether a shared Crew can keep this host awake, and whether it is a laptop. */
  keep_awake?: KeepAwakeCapability
}

export type TailnetDirectState = 'active' | 'off' | 'unavailable'

/** The direct tailnet listener as the gateway started it. `urls` are plain
 *  share links (no token); `method` is how the gateway reaches the local
 *  Tailscale daemon. */
export interface TailnetDirect {
  state: TailnetDirectState
  reason: string
  method: string
  urls: string[]
  owner_login: string
  addresses: string[]
}

export type SetupPath =
  | 'tailscale'
  | 'kiro_login'
  | 'systems_manager'

export type SetupPathReason =
  | 'not_configured'
  | 'not_in_this_build'
  | 'tailnet_unreachable'
  | 'source_failed'

export interface SetupPathCapability {
  state: CapabilityState
  /** A `SetupPathReason` when not available, else empty. */
  reason: SetupPathReason | ''
}

export type IdentityClass =
  | 'organization_directory'
  | 'individual'
  | 'tailnet'

export type AccessRequirement = 'cloud_account' | 'individual_login'

/** Shared multi-user eligibility. Decided by an external authority, never by
 *  identity class; `unavailable` (`not_configured`) means no person is granted
 *  shared access through it. */
export interface SharedEligibility {
  decided_by: 'external_authority'
  state: CapabilityState
  reason: EnterpriseUnavailableReason | ''
}

export interface AccessMode {
  /** For shared multi-user access: the network setup path, not eligibility. */
  state: CapabilityState
  requires: AccessRequirement
  /** Personal remote control only: every human class. */
  identity_classes?: IdentityClass[]
  /** Shared multi-user access only. An older gateway omits it. */
  eligibility?: SharedEligibility
  /** Personal remote control only: whether it is on before the operator chooses. */
  default_enabled?: boolean
}

export interface AccessModes {
  shared_multi_user: AccessMode
  personal_remote_control: AccessMode
  agent_workload_identity: AccessMode
}

export interface WorkloadIdentityHost {
  supported_hosts: string[]
  host: string
  host_supported: boolean
  /** `host_not_supported` on any host outside `supported_hosts`. */
  reason: '' | 'host_not_supported'
  /** An older gateway omits it. */
  pod_boundary?: PodBoundary
}

/** Any process in the attested pod can obtain the workload identity. */
export interface PodBoundary {
  scope: 'any_process_in_pod'
  per_process_isolation: false
}

/** The Crew's health: exactly three values. A Crew that never connected is
 *  `not_connected`. Telemetry only: it never grants or revokes access. */
export type HeartbeatHealth =
  | 'healthy'
  | 'heartbeat_late'
  | 'not_connected'

export interface HeartbeatSnapshot {
  state: HeartbeatHealth
  /** Epoch seconds of the last acknowledged heartbeat. */
  last_success: number | null
  /** Epoch seconds of the last heartbeat tried, acknowledged or not. */
  last_attempt: number | null
}

/** A Crew's state in a directory listing or after a failed connection.
 *  Access loss (`revoked`, `sign_in_expired`) is never shown as `offline`. */
export type CrewConnectionState =
  | 'available'
  | 'revoked'
  | 'offline'
  | 'relay_unavailable'
  | 'sign_in_expired'
  | 'host_disabled'
  | 'version_incompatible'

export interface DirectoryEntry {
  crew_id: string
  display_name: string
  state: CrewConnectionState
}

export type DirectoryErrorCode =
  | 'not_configured'
  | 'directory_unavailable'
  | 'sign_in_expired'
  | 'relay_unavailable'
  | 'version_incompatible'
  | 'malformed'

/** An empty listing and an error are different answers. */
export type DirectoryAnswer =
  | { status: 'ok'; entries: DirectoryEntry[] }
  | { status: 'error'; error: DirectoryErrorCode }

export type CapabilityField =
  | 'attribution'
  | 'presence'
  | 'queueing'
  | 'containment'
  | 'containment_level'
  | 'authoritative_directory'
  | 'membership_link'
  | 'remote_actions'

/** `no_source` / `source_failed` for any field; the rest say why `attribution`
 *  cannot name a person reaching this Crew through `tailscale serve`. */
export type CapabilityReason =
  | 'no_source'
  | 'source_failed'
  | 'platform_unprovable'
  | 'daemon_same_user'
  | 'daemon_not_found'

export type EnterpriseCapabilityKey =
  | 'human_credentials'
  | 'individual_grants'
  | 'workload_identity'
  | 'crew_identity'
  | 'heartbeat'
  | 'inference_budget'
  | 'deployment_context'
  | 'human_sign_in'
  | 'revocation'
  | 'workload_verifier'
  | 'crew_registration'
  | 'directory'
  | 'channel_actors'

export type EnterpriseUnavailableReason =
  | 'not_configured'
  | 'provider_refused'
  | 'test_provider_refused'
  | 'provider_failed'
  | 'requires_workload_identity'
  | 'host_not_supported'

export interface EnterpriseCapability {
  state: CapabilityState
  /** An `EnterpriseUnavailableReason` when unavailable, else empty. */
  reason: EnterpriseUnavailableReason | ''
}

export type OperationKind =
  | 'chat_turn'
  | 'session_start'
  | 'resume'
  | 'subagent'
  | 'workflow'
  | 'task'
  | 'cron'
  | 'channel_turn'
  | 'remote'
  | 'background'

export type OperationState =
  | 'submitted'
  | 'queued'
  | 'admitted'
  | 'running'
  | 'cancelling'
  | 'reconciling'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired'
  | 'rejected'
  | 'unknown_outcome'

export const TERMINAL_STATES: ReadonlySet<OperationState> = new Set<OperationState>([
  'completed',
  'failed',
  'cancelled',
  'expired',
  'rejected',
  'unknown_outcome',
])

/** Mirrors `admission.TRANSITIONS`; a test pins the two equal. */
export const OPERATION_TRANSITIONS: Readonly<Record<OperationState, readonly OperationState[]>> = {
  submitted: ['queued', 'admitted', 'rejected'],
  queued: ['admitted', 'cancelled', 'expired', 'reconciling'],
  admitted: ['running', 'queued', 'cancelled', 'expired', 'reconciling', 'failed'],
  running: ['cancelling', 'completed', 'failed', 'reconciling'],
  cancelling: ['cancelled', 'completed', 'failed', 'reconciling'],
  reconciling: ['queued', 'running', 'completed', 'failed', 'cancelled', 'unknown_outcome'],
  completed: [],
  failed: [],
  cancelled: [],
  expired: [],
  rejected: [],
  unknown_outcome: [],
}

export function isLegalTransition(from: OperationState, to: OperationState): boolean {
  return OPERATION_TRANSITIONS[from].includes(to)
}

export type ReasonCode =
  | 'principal_active_limit'
  | 'principal_queue_full'
  | 'crew_active_limit'
  | 'crew_queue_full'
  | 'session_create_rate'
  | 'request_too_large'
  | 'storage_limit'
  | 'containment_unavailable'
  | 'queue_expired'
  | 'run_deadline'
  | 'runtime_error'
  | 'grant_revoked'
  | 'policy_changed'
  | 'cancelled_by_user'
  | 'access_removed'
  | 'stale_revision'
  | 'already_resolved'
  | 'idempotency_conflict'
  | 'invalid_transition'
  | 'not_found'
  | 'send_race'
  | 'composer_locked'

/** `AcceptedWork.to_public_dict`. */
export interface AcceptedWork {
  work_id: string
  session_key: string
  operation: OperationKind
  state: OperationState
  revision: number
  accepted_at: number
  expires_at: number
  deadline_at: number | null
  reason: ReasonCode | null
}

export interface ScopeUsage {
  active: number
  queued: number
  /** `null` means the scope is not limited on this host. */
  active_limit: number | null
  queue_limit: number | null
}

/** No wait-time estimate exists by design; `retry_after_seconds` is a polling hint. */
export interface CapacityResponse {
  schema_version: number
  principal: ScopeUsage
  crew: ScopeUsage
  enforcement: EnforcementLevel
  limiting_scope: 'principal' | 'crew' | null
  reason: ReasonCode | null
  retry_after_seconds: number | null
  queue_expiry_seconds: number | null
  run_deadline_seconds: number | null
}

/** Refusal / conflict body: the dashboard's `{error, code}` envelope plus context. */
export interface SharedCrewErrorBody {
  error: string
  code: ReasonCode
  capacity?: CapacityResponse
  current?: AcceptedWork
}

/** One send the host accepted into a session (`send_race.AcceptedSend`). */
export interface AcceptedSend {
  revision: number
  principal: PublicPrincipal
  message_id: string
  accepted_at: number
}

/** A session's send revision: the GET `/api/shared-crew/send-state` body and the
 *  `shared_crew.send_state` frame. A send carries it back as `base_revision`. */
export interface SendState {
  schema_version: number
  session_key: string
  revision: number
  last: AcceptedSend | null
}

/** The 409 answer to a send that lost a first-submit-wins race. Nothing was
 *  accepted: no queue entry, no steer, no turn. The draft stays with the sender. */
export interface SendRaceConflictBody {
  error: string
  code: 'send_race'
  session_key: string
  current_revision: number
  winner: AcceptedSend
}

/** Who holds a session's composer typing lease: the GET
 *  `/api/shared-crew/typing-lease` body and the `shared_crew.typing_lease`
 *  frame. While someone else holds it this tab's composer is read-only and the
 *  host refuses its send with `composer_locked`. `holder_ids` lists every id
 *  that counts as the holder (one operator's local sign-ins fold into one). */
export interface TypingLease {
  schema_version: number
  session_key: string
  holder: PublicPrincipal | null
  holder_ids: string[]
  /** Wall-clock seconds when the lease ends unless renewed; null when free. */
  expires_at: number | null
  idle_seconds: number
  /** Why the lease just ended (`sent`, `cleared`, `idle`, `disconnected`), or ''. */
  ended: string
}

/** The 409 answer to a send while someone else holds the typing lease. Nothing
 *  was accepted and the draft stays with the sender. */
export interface ComposerLockedBody {
  error: string
  code: 'composer_locked'
  session_key: string
  holder: PublicPrincipal
  lease: TypingLease
}

export type Activity = 'viewing' | 'typing' | 'idle'

export interface PresenceEntry {
  principal: PublicPrincipal
  session_key: string
  activity: Activity
  connections: number
  expires_at: number
}

export type DecisionKind = 'approval' | 'question'
export type DecisionState = 'pending' | 'resolved' | 'expired' | 'withdrawn'

/** `SharedDecision.to_public_dict`. */
export interface SharedDecision {
  decision_id: string
  kind: DecisionKind
  session_key: string
  state: DecisionState
  revision: number
  initiator: PublicPrincipal
  responder: PublicPrincipal | null
  outcome: string
  resolved_at: number | null
}

export type SharedCrewServerFrame =
  | { type: 'shared_crew.presence'; schema_version: number; session_key: string; entries: PresenceEntry[] }
  /** `slot_key`: the dashboard tab showing the work's session, when one does. */
  | { type: 'shared_crew.work'; schema_version: number; work: AcceptedWork; slot_key?: string }
  | { type: 'shared_crew.decision'; schema_version: number; decision: SharedDecision }
  | { type: 'shared_crew.capacity'; schema_version: number; capacity: CapacityResponse }
  | ({ type: 'shared_crew.send_state' } & SendState)
  | ({ type: 'shared_crew.typing_lease' } & TypingLease)
  | { type: 'shared_crew.memory_change'; schema_version: number; change: MemoryChange }

/** `memory_changes.MemoryChange.payload`: a lesson write that replaced stored
 *  ones (newest wins). Summaries are clipped and redacted by the server. */
export interface MemoryChange {
  actor: { id: string; kind: string; name: string }
  session_key: string
  category: string
  new: string
  old: string[]
}

/** Client activity report. Carries no principal: the server stamps the connection's actor. */
export interface SharedCrewActivityFrame {
  type: 'shared_crew.activity'
  session_key: string
  activity: Activity
  /** Set when this tab's draft is empty, which releases its typing lease. A
   *  content-free flag: the draft itself never leaves the tab. */
  draft?: 'empty'
}
