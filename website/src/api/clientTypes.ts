export type PasteExchange = {
  reached: boolean
  ok: boolean
  tokenAccepted: boolean
  ownerOk: boolean
}

export interface SessionEnvironmentProfile {
  id: string
  name: string
  provider: string
  workdir: string
  /** Template name, what tells two profiles apart; older gateways omit it. */
  template?: string
  /** Kiro Cloud profiles only: the repositories its cloud sandbox opens. */
  repositories?: string[]
  /** Connected computers only (`provider: 'connected_computer'`): its connector is live now. */
  connected?: boolean
  /** Connected computers only: another computer has the same name, so show its id (B09). */
  label_is_shared?: boolean
}

export interface RemoteDirListing {
  root: string
  path: string
  entries: { name: string; dir: boolean }[]
  truncated: boolean
}

export interface RemoteFileContent {
  root: string
  path: string
  content: string
  binary: boolean
  truncated: boolean
}

export interface RemoteEnvironmentHealth {
  state: 'running' | 'stopped' | 'unavailable' | 'not_created'
  workspace?: string
  workdir?: string
  error?: string
  stats?: {
    arch: string
    cpus: number
    load?: number[]
    memory?: { total_bytes: number; available_bytes: number }
    disk?: { total_bytes: number; free_bytes: number }
    uptime_s?: number
  }
}

export interface RemoteGitResult {
  root: string
  /** False for a non-repository, or a failure accompanied by error_text. */
  git: boolean
  output?: string
  code?: 'git_failed'
  /** Subprocess failure only; a non-repository or unborn branch is not an error. */
  error_text?: string
  truncated?: boolean
}

export type GitLoginStatus = 'signed_in' | 'not_signed_in' | 'unchecked' | 'withheld' | 'unsupported'

export interface SessionEnvironmentsCheck {
  coder: { ok: boolean; owner?: string; error?: string }
  profiles: { id: string; ok: boolean; error?: string }[]
  /** Per saved Sprites profile: API token saved and accepted. Older gateways omit it. */
  sprites?: { id: string; ok: boolean; error?: string }[]
  /** Per saved boxd profile: CLI installed and signed in. Older gateways omit it. */
  boxd?: { id: string; ok: boolean; error?: string }[]
  /** Absent from a gateway that predates the git rows. */
  git?: Record<string, GitLoginStatus>
}

export interface SessionEnvironmentWorkspace {
  workspace: string
  /** Older gateways omit it; those rows are Coder workspaces. */
  provider?: 'coder' | 'sprites' | 'boxd'
  slot: string
  /** The profile the chat is bound to; empty when unknown. */
  profile?: string
  state: string
  template: string
  created_at: string
  /** Set once the owning session is archived: when the workspace is deleted. */
  deletion_due_at: string
}

export type SessionEnvironmentStartStage = 'checking' | 'provisioning' | 'connecting' | 'starting_agent'

export interface SlotEnvironment {
  /** Recorded placement survives a profile being removed or reused. */
  provider?: string | null
  profile: string | null
  profiles: SessionEnvironmentProfile[]
  /** True once the session has a message: its environment can no longer change. */
  locked?: boolean
  /** A runtime in this gateway is attached to the remote workspace. */
  attached?: boolean
  /** Set while this session's environment is starting in THIS gateway. */
  starting?: {
    stage: SessionEnvironmentStartStage
    stage_elapsed_s: number
    elapsed_s: number
    /** Every stage this start has entered, in order. Older gateways omit it. */
    entered?: SessionEnvironmentStartStage[]
    /** The gateway already had this session attached: an engine restart. */
    reconnect?: boolean
  } | null
}

export type BenignDenial = { readonly status: number; readonly code: string }

export interface KiroBonusCreditGrantPayload {
  name: string
  used: number
  total: number
  days_left?: number
}

export interface KiroUsagePayload {
  available?: boolean
  /**
   * Why usage is unavailable when `available` is false (`signin_required`,
   * `account_unproven` when the account is named but its reading could not be
   * tied to it, or `api_key` when the sessions run on a `KIRO_API_KEY` whose
   * balance cannot be read); absent when the gateway simply holds no reading.
   */
  reason?: string
  /** With `reason: signin_required`: the API refused a credential it was shown. */
  credential_rejected?: boolean
  credits_used?: number
  credits_covered?: number
  credits_overage?: number
  credits_plan?: number
  resets?: string
  plan?: string
  cost_usd?: number
  overage_rate?: number | string
  bonus_credits?: KiroBonusCreditGrantPayload[]
  stale?: boolean
  account?: string
  email?: string
  account_type?: string
  start_url?: string
}

export interface KiroUsageRefreshResponse {
  usage?: KiroUsagePayload
  skipped?: 'scrape_parked'
  retry_after?: number
}

export interface KiroBonusCreditGrant {
  name: string
  used: number
  total: number
  daysLeft?: number
}

export interface KiroCreditUsage {
  used: number
  limit: number
  overage: number
  resets?: string
  plan?: string
  costUsd?: number
  overageRate?: number
  bonusCredits: KiroBonusCreditGrant[]
  stale: boolean
  account?: string
  email?: string
  accountType?: string
  startUrl?: string
}
