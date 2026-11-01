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

import type { PublicPrincipal } from '../types'

// The attribution shapes a session row carries live with the shared types
// (`types/index.sharedCrew.ts`); they are re-exported here with the rest of the
// Shared Crew wire contracts.
export type {
  AttributionAvailability,
  DisplaySnapshot,
  PrincipalKind,
  PublicPrincipal,
  SessionAttribution,
  SessionTrigger,
  SessionTriggerKind,
} from '../types'

export const SHARED_CREW_SCHEMA_VERSION = 1 as const

export const UNKNOWN_PRINCIPAL_ID = 'p_unknown'

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
  /** Each shared-access setup path's availability. An older gateway omits it. */
  setup_paths?: Record<SetupPath, SetupPathCapability>
  /** The direct tailnet listener. An older gateway omits it. */
  tailnet_direct?: TailnetDirect
  /** Which verified path names people: `tailscale_direct`, or empty. */
  attribution_via?: '' | 'tailscale_direct'
  /** Whether a shared Crew can keep this host awake, and whether it is a laptop. */
  keep_awake?: KeepAwakeCapability
  /** Whether Tailscale invites can be used. `governance_pinned_off`: an
   *  enterprise policy pins tailnet access off, and the invite routes answer 404.
   *  An older gateway omits it. */
  invites?: { state: 'available' | 'unavailable'; reason: '' | 'governance_pinned_off' }
  /** `true` only while the gateway runs with `KIROCREW_SHARED_CREW_PREVIEW=1`.
   *  Off, or omitted by an older gateway, the sharing surfaces stay hidden. */
  shared_crew_preview?: boolean
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

export type OperationKind =
  | 'chat_turn'
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
  /** Why the lease just ended (`sent`, `cleared`, `idle`, `disconnected`,
   *  `taken_over`), or ''. */
  ended: string
  /** Only on the frame (and takeover answer) announcing a takeover, which needs
   *  `KIROCREW_SHARED_CREW_PREVIEW=1`: who held the lease before, and every id
   *  that counts as them. */
  taken_over_from?: PublicPrincipal | null
  taken_over_from_ids?: string[]
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
  /** `host`: the gateway settled it (an approval mode such as trust or YOLO),
   *  so no person decided. An older gateway omits it. */
  settled_by?: 'person' | 'host'
}

/** `GET`/`PUT /api/shared-crew/preview`: the owner's Feature preview switch.
 *  `enabled` is the stored choice, `active` what this gateway started with,
 *  `env_override` true while `KIROCREW_SHARED_CREW_PREVIEW=1` forces it on. */
export interface SharedCrewPreviewSetting {
  enabled: boolean
  active: boolean
  env_override: boolean
  restart_required: boolean
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
  /** Someone changed a person's display name or picture: People refetches. */
  | { type: 'shared_crew.people'; user_id: string }

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
