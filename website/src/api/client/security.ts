/**
 * Settings > Security and the credential vault: posture and stats, denied
 * commands, redaction allowed hosts, third-party app trust, the read-only
 * governance policy, vault secrets, and the paid-AWS, flagged-file-delivery
 * and credential-redaction consents.
 */

import { relocateRequestUrl } from '../../lib/dashboardRuntime'
import type { ClientTransport } from './transport'

/** A built-in denied-command rule as returned by GET /api/security/denied-commands. */
export interface DeniedCommandRule {
  id: string
  pattern: string
  category: string
  description: string
  enabled: boolean
  pinned: boolean
  /** Why the rule is locked (forced-on, non-toggleable): 'floor' = enforced by
   *  an always-on floor built into Kiro Crew; 'policy' = governance-pinned;
   *  null/absent = freely toggleable. Additive — `pinned` keeps its
   *  governance-only meaning. */
  lock_reason?: 'floor' | 'policy' | null
  /** Where the rule came from: 'builtin' = shipped in Kiro Crew's catalog;
   *  'edition' = contributed by a composed edition through the `denied_rules`
   *  seam. Edition rules are default-on and freely toggleable (never locked),
   *  and are grouped by their own `category`, so the panel needs no special
   *  case. Optional so an older gateway's response still type-checks. */
  source?: 'builtin' | 'edition'
}

/** A user-authored denied-command pattern. */
export interface DeniedUserRule {
  id: string
  pattern: string
  enabled: boolean
  /** Operator prose shown in the refusal when this rule fires. Optional: rules
   *  added before the field existed, and rules added without one, omit it. */
  note?: string
}

/** What a paid AWS service would bill, and whether the operator confirmed it. */
export interface AwsConsentStatus {
  service: string
  serviceLabel: string
  /** Configured profile name. Empty means the provider's own default chain. */
  profile: string
  /** Human-readable rendering of `profile` for display. */
  credentialSource: string
  region: string
  account: string
  arn: string
  identityResolved: boolean
  identityDetail: string
  granted: boolean
  /** Operator-facing explanation when `granted` is false. */
  reason: string
  /** True when this GET withdrew a stale grant because the account changed. */
  revokedOnAccountChange: boolean
  grant: { account: string; region: string; profile: string; granted_at: string } | null
}

/** One recorded consent to deliver scanner-flagged files to a destination class. */
export interface FileDeliveryGrant {
  destination_class: string
  granted_at: string
}

/**
 * Which flagged-file delivery destinations the owner has confirmed.
 *
 * Every list here is SERVER-OWNED and rendered as returned. The panel keeps no
 * copy of which classes are grantable, so a class moving between `grantable` and
 * `never_grantable` cannot leave the UI offering a control the backend would
 * refuse — the handler validates `destination_class` against its own grantable
 * set and answers `unknown_destination_class` otherwise.
 *
 * `grants` is keyed by destination class with `null` meaning "not confirmed",
 * which is the same fail-closed reading the backend applies to a missing or
 * unparseable record.
 */
export interface FileDeliveryConsentStatus {
  ok?: boolean
  grantable: string[]
  never_grantable: string[]
  labels: Record<string, string>
  grants: Record<string, FileDeliveryGrant | null>
}

/**
 * The owner's credential-redaction switch (Settings > Security > Credential
 * redaction). `enabled` is the position as RECORDED by the backend, which reads a
 * missing or unreadable record as `true`; `changed_at` is empty until the owner
 * has flipped it at least once.
 */
export interface CredentialRedactionState {
  enabled: boolean
  changed_at: string
}

/** The SPA-safe view of an armed grant request. The approval NONCE is never
 *  sent to the browser: finishing the grant needs `approve_command` run on the
 *  host, which is the human-presence proof an agent-driven browser cannot fake. */
export interface ArmedFileDeliveryConsent {
  ok?: boolean
  armed: boolean
  request_id?: string
  destination_class?: string
  expires_in?: number
  approve_command?: string
}

/** Full denied-commands snapshot returned by every denied-commands endpoint. */
export interface DeniedCommandsData {
  builtins: DeniedCommandRule[]
  user_added: DeniedUserRule[]
  disable_all: boolean
  effective_count: number
  governance_locked: boolean
}

/** Serialized effective control for one governed scope (archetype-specific). */
export interface GovernanceScopeDetail {
  /** ruleset / scopedmap-members / capability-inner: the set MODE plus how many
   *  entries it holds. POSTURE ONLY — the endpoint deliberately never sends the
   *  rule CONTENTS (allow/deny globs, command patterns) to the browser, because
   *  the dashboard is reachable by the agent's own Playwright tooling and the
   *  exact deny patterns are the security ceiling the agent is fenced from. The
   *  human operator reads the authoritative rules from the policy files directly. */
  mode?: string
  allow_count?: number
  deny_count?: number
  /** ruleset intersection (allow∩ that can't flatten): the composed halves. */
  components?: GovernanceScopeDetail[]
  /** ordinal: the enforced floor value + its strictness scale. */
  scale?: string
  floor?: string
  /** capability: on/off + inner allowlists (e.g. spawn→agents). */
  enabled?: boolean
  inner?: Record<string, GovernanceScopeDetail>
  /** scopedmap (channels): allowed members + per-member posture. */
  members?: GovernanceScopeDetail
  posture?: Record<string, Record<string, GovernanceScopeDetail>>
}

/** One row of the effective governance ceiling for a single scope. */
export interface GovernanceScope {
  scope: string
  archetype: 'ruleset' | 'ordinal' | 'capability' | 'scopedmap'
  /** false = neither policy nor profile governs it → the scope permits. */
  governed: boolean
  source: 'policy' | 'profile' | 'policy+profile' | 'ungoverned'
  /** WHOSE ceiling this row describes, so a host-only pin is not read as
   *  install-wide. `host_profile` = the host-surface profile contributes, so the
   *  value is that ONE surface's posture (the host profile disables cron and
   *  messaging because the host process performs neither; the cron and messaging
   *  surfaces enable them under their own profiles). `policy_wide` = policy alone
   *  governs, which applies to every surface. Absent/'' = ungoverned. */
  scope_note?: '' | 'host_profile' | 'policy_wide'
  detail: GovernanceScopeDetail
}

/** Central-policy-distribution posture, from the `distribution` key of
 *  GET /api/governance/policy.
 *
 *  POSTURE ONLY, and one field more strictly than the rest of that payload: the
 *  producer reports the source's SCHEME and never its URL, because the endpoint is
 *  the fleet's control plane and the dashboard is reachable by the agent's own
 *  browser tooling. Every value is a number, a boolean, or a machine-readable enum,
 *  so the dashboard renders translated text and no English arrives in the JSON body.
 *
 *  Only `configured` is guaranteed: the producer's fail-safe path answers
 *  `{configured: false}` alone when the posture itself could not be resolved, so
 *  every other field is optional and a reader must treat absence as unknown rather
 *  than as a zero. */
export interface GovernanceDistributionData {
  /** Whether this host fetches its ceiling from a central source at all. False
   *  also covers "could not tell" — pair it with `error_code`. */
  configured: boolean
  /** URL scheme of the source (`https`, `file`, `http`), never the URL. '' when
   *  unconfigured. */
  source_scheme?: string
  /** Seconds between background re-fetches, already clamped to the polling floor.
   *  0 = fetched at boot only. */
  refresh_interval_seconds?: number
  /** The fleet's staleness bound on the cached copy. 0 = no bound. */
  max_cache_age_seconds?: number
  /** What the host does when no ceiling can be established: refuse to start, or
   *  fall through to the next policy tier and report a governance incident. */
  on_unavailable?: 'fail_closed' | 'degrade' | ''
  /** Whether the background poll loop is alive in this process. */
  refresher_running?: boolean
  /** Whether a last-known-good copy recorded against THIS source is on disk. */
  cache_present?: boolean
  /** Age of that copy in seconds; null when there is none. */
  cache_age_seconds?: number | null
  /** Outcome of the most recent background refresh. '' before the first one. */
  last_refresh_status?: 'not_configured' | 'unchanged' | 'applied' | 'rejected' | 'unreachable' | ''
  /** Seconds since that refresh; null when none has run. */
  last_refresh_age_seconds?: number | null
  /** Why the posture could not be resolved, as an enum the dashboard maps to
   *  translated copy. '' when nothing went wrong. */
  error_code?: 'misconfigured' | ''
}

/** GET /api/governance/policy — the read-only effective ceiling across scopes. */
export interface GovernancePolicyData {
  /** Policy schema version, or null when no enterprise ceiling is present. */
  version: number | null
  /** Whether a Level-1 enterprise ceiling is in effect at all. */
  has_policy: boolean
  /** The bound host-surface profile name, or null. */
  profile: string | null
  /** The surface this snapshot resolved (always "host"); narrower per-surface/
   *  app/task profiles can tighten a scope further at runtime. */
  surface?: string
  /** Surfaces OTHER than host that carry their own bound profile — names only.
   *  Rendered so a reader can see that a host row's "disabled" is one surface's
   *  posture, not the whole install's. */
  other_bound_surfaces?: string[]
  /** True when the resolved profile is a deny-all fallback because the file
   *  could not be read or parsed — enforcement is correct (fail-closed) but the
   *  operator should know the ceiling is synthetic, not intentional. */
  fallback_profiles?: string[]
  /** Capability scopes a profile names that this build does not register —
   *  typically scopes a companion edition adds, though a misspelled scope key
   *  lands here too. Keyed by profile stem, sorted scope names as values;
   *  present only for profiles carrying such scopes, and deliberately NOT
   *  narrowed to the host profile — every loaded profile reports. Producer:
   *  the governance security payload (PR #5544). Tolerated at load time and
   *  inert in this build. */
  unknown_profile_scopes?: Record<string, string[]>
  /** Where the ceiling itself comes from, when a central source publishes it.
   *  Present on BOTH the normal and the fail-safe snapshot, so a host whose first
   *  fetch has not landed — `has_policy: false`, `profile: null` — can still be told
   *  apart from a host that has no enterprise ceiling at all. */
  distribution?: GovernanceDistributionData
  /** True when governance resolution failed — the viewer shows a soft notice. */
  unavailable: boolean
  scopes: GovernanceScope[]
}

/** One concrete element behind a security-posture count. */
export interface PostureItem {
  /** What the control covers — a blocked path, a redaction sink, a credential family. */
  label: string
  /** Optional "how/where" secondary text. */
  detail: string
}

/** One expandable security control from GET /api/security/posture.
 *
 *  POSTURE ONLY, by the same contract as the governance viewer: items are public
 *  control definitions (blocked path patterns, redaction sink modules, credential
 *  FAMILY names) and derived counts — never credential material, governance rule
 *  contents, or user data. `count` is `items.length` server-side, so a pill can
 *  never drift from the list it summarizes. */
export interface PostureControl {
  key: string
  label: string
  /** Noun for the count, e.g. "output paths" — rendered as `${count} ${unit}`. */
  unit: string
  summary: string
  /** Repo-relative path of the module that enforces the control. */
  source: string
  /** null when the control could not be resolved (see `unavailable`). */
  count: number | null
  items: PostureItem[]
  /** True when this control's detail could not be resolved; the rest still render. */
  unavailable: boolean
}

/** GET /api/security/posture — expandable detail behind every posture count. */
export interface SecurityPostureData {
  controls: PostureControl[]
  /** Flat `key → count` map for callers that only need the pill values. */
  counts: Record<string, number | null>
}

/**
 * GET /api/security/trusted-apps — per-app grants that let a third-party app
 * run its own code (Python in-process, its own backend, manifest shell
 * commands).  Third-party app code is refused by default; a grant is made for
 * ONE app at a time from the trust-consent modal, so `apps` is the explicit
 * allow list and `allowAll` is the separate blanket escape hatch.
 */
export interface TrustedAppsData {
  /** Grants the execution gate ACTUALLY enforces (valid app names, sorted). */
  apps: string[]
  /**
   * Entries stored in `config.json` that the gate IGNORES because they fail the
   * app-name charset — a hand-edited config can hold `LD-App`, `ld-app ` with a
   * trailing space, a fullwidth homoglyph, `..` or `*`. They must render
   * separately from `apps`: folded in, the panel claims trust that does not
   * exist and the user cannot tell why their app is still blocked.
   */
  ineffective: string[]
  /** Blanket grant — trusts every third-party app, present or future. */
  allowAll: boolean
}

/**
 * DELETE /api/security/trusted-apps/{name} — the refreshed snapshot PLUS whether
 * the revoke also had to DISABLE the app. Revoking trust has to stop the app's
 * code from running, so the backend disables a currently-enabled app in the same
 * transaction; the UI must say so, otherwise an app silently stops working.
 */
export interface TrustedAppsRevokeResult extends TrustedAppsData {
  disabled: boolean
}

/** One credential the gateway knows how to use by name, as `GET /api/secrets`
 *  reports it. `host` is present only for a per-host credential. */
export interface ManagedSecret {
  name: string
  kind: string
  host?: string
}

/** What `GET /api/secrets` answers with.
 *
 *  Values are never on this wire: the vault is write-only to the dashboard, so
 *  the list carries NAMES plus the catalog rows that describe them.
 *
 *  Typed HERE rather than in the panel that renders it because the request
 *  belongs on this transport. The panel used to issue its own `fetch`, which
 *  reached none of the recovery `j` runs, so an expired session read a bare
 *  status code with no way to sign back in (#12240).
 */
export interface SecretsListResponse {
  names: string[]
  managed: ManagedSecret[]
  /** Stored names the gateway currently ignores, with the reason. Absent when
   *  every stored name is live. */
  unused?: Array<{ name: string; reason: 'wakatime_disabled' | 'jira_multi_host' | 'jira_host_precedence' }>
  /** The managed catalog could not be read. Carried on a 200: the stored names
   *  are still authoritative, so this is a warning beside a good list rather
   *  than a failed request. */
  managed_error?: boolean
}

export function createSecurityEndpoints({ get, post, put, del, patch, j }: ClientTransport) {
  const posture = {
    // Counts are derived server-side from the controls they describe, so a null
    // means "temporarily unresolvable", never "zero".
    securityStats: () => get('/api/security/stats').then(j) as Promise<{ denied_commands: number | null; suspicious_patterns: number | null; tool_schemas: number | null; redaction_paths: number | null }>,
    securityPosture: () => get('/api/security/posture').then(j) as Promise<SecurityPostureData>,
  }

  const policies = {
    // Denied commands (Settings → Security). Every endpoint returns the full
    // refreshed snapshot so callers can seed their query cache from the response.
    deniedCommands: () => get('/api/security/denied-commands').then(j) as Promise<DeniedCommandsData>,
    toggleBuiltinDeniedCommand: (id: string, enabled: boolean) =>
      patch('/api/security/denied-commands/builtins/' + encodeURIComponent(id), { enabled }).then(j) as Promise<DeniedCommandsData>,
    setDeniedCommandsDisableAll: (value: boolean) =>
      patch('/api/security/denied-commands/disable-all', { value }).then(j) as Promise<DeniedCommandsData>,
    addUserDeniedCommand: (pattern: string, note = '') =>
      post('/api/security/denied-commands/user', { pattern, note }).then(j) as Promise<DeniedCommandsData>,
    toggleUserDeniedCommand: (id: string, enabled: boolean) =>
      patch('/api/security/denied-commands/user/' + encodeURIComponent(id), { enabled }).then(j) as Promise<DeniedCommandsData>,
    deleteUserDeniedCommand: (id: string) =>
      del('/api/security/denied-commands/user/' + encodeURIComponent(id)).then(j) as Promise<DeniedCommandsData>,
    // Redaction cards: the per-workspace allowed-host list (Settings → Security →
    // Redaction). Every route is owner-only.
    redactionAllowedHosts: () =>
      get('/api/redaction/allowed-hosts').then(j) as Promise<{ workspaces: Record<string, string[]> }>,
    redactionAllowHost: (slot: string, host: string) =>
      post('/api/redaction/allowed-hosts', { slot, host }).then(j) as Promise<{ ok: boolean; workspace: string }>,
    redactionRevokeHost: (workspace: string, host: string) =>
      del(`/api/redaction/allowed-hosts?workspace=${encodeURIComponent(workspace)}&host=${encodeURIComponent(host)}`)
        .then(j) as Promise<{ ok: boolean; removed: boolean }>,
    // Third-party app trust (Settings → Security). Like denied-commands, every
    // endpoint returns the full refreshed snapshot so callers can seed the query
    // cache from the mutation response instead of re-fetching.
    listTrustedApps: () => get('/api/security/trusted-apps').then(j) as Promise<TrustedAppsData>,
    trustApp: (name: string, repository?: string) =>
      post(
        '/api/security/trusted-apps/' + encodeURIComponent(name),
        repository ? { repository } : undefined,
      ).then(j) as Promise<TrustedAppsData>,
    // Returns the snapshot PLUS `disabled` — revoking trust also disables an app
    // that is currently enabled, so its code stops running immediately.
    untrustApp: (name: string) =>
      del('/api/security/trusted-apps/' + encodeURIComponent(name)).then(j) as Promise<TrustedAppsRevokeResult>,
    setTrustAllApps: (value: boolean) =>
      put('/api/security/trusted-apps/allow-all', { value }).then(j) as Promise<TrustedAppsData>,
    // Read-only governance policy viewer (Settings → Security). No write path —
    // the enterprise ceiling is file-authored and un-editable via the UI.
    governancePolicy: () => get('/api/governance/policy').then(j) as Promise<GovernancePolicyData>,
  }

  const secrets = {
    /** Vault secret NAMES (values are never exposed). Same endpoint the Settings
     * Secrets panel reads.
     *
     * On `get` rather than a bare `fetch` so the request carries `X-Session-Key`
     * and the server-side ephemeral gate always runs, matching every other method
     * here. */
    secretsList: () => get('/api/secrets').then(j) as Promise<SecretsListResponse>,
    /** Store a vault secret under *name*, replacing any current value.
     *
     *  On this transport rather than the Secrets panel's own `fetch`, which is what
     *  every sibling settings panel already does and what the panel gains by it:
     *  the `X-Session-Key` header, `checkSessionExpired`'s silent refresh and
     *  re-auth banner, and an `ApiError` whose message is the sign-in instruction
     *  instead of the gateway's cryptographic reason. Raw `fetch` reaches none of
     *  that, so an expired session was shown a bare status code and offered only a
     *  retry that could not succeed (#12240). */
    secretsSave: (name: string, value: string) => post('/api/secrets', { name, value }).then(j),
    /** Remove a vault secret by name. Same transport reason as `secretsSave`. */
    secretsDelete: (name: string) => del('/api/secrets/' + encodeURIComponent(name)).then(j),
  }

  const consents = {
    // Paid-AWS-service consent (Amazon Polly for TTS, Amazon Transcribe for STT).
    // The GET reports what would be billed AND performs the identity probe, so it
    // is the call that surfaces the account before the operator agrees to it.
    awsConsent: (service: string) =>
      fetch(relocateRequestUrl('/api/aws/consent?service=' + encodeURIComponent(service))).then(j) as Promise<AwsConsentStatus>,
    grantAwsConsent: (service: string, shown: { profile: string; region: string; account: string }) =>
      post('/api/aws/consent', {
        service,
        // Echo back exactly what was on screen. The backend rejects a mismatch, so
        // a confirmation can only ever apply to the account the operator read.
        expectedProfile: shown.profile,
        expectedRegion: shown.region,
        expectedAccount: shown.account,
      }).then(j) as Promise<{ ok?: boolean; error?: string; code?: string; identityDetail?: string }>,
    revokeAwsConsent: (service: string) =>
      del('/api/aws/consent?service=' + encodeURIComponent(service)).then(j) as Promise<{ ok?: boolean; removed?: boolean }>,

    // Flagged-file delivery consent (Settings > Security > Flagged-file delivery).
    // FOUR EXPLICIT VERBS, deliberately not one helper that takes a method: the
    // handler re-applies the owner gate on each separately, and a caller that
    // collapsed them onto a shared path would be expressing a read and a write as
    // the same grant. Read #8514 for why that shape is a hazard -- a fence keyed on
    // a path PREFIX cannot tell the verbs apart, so admitting the read admits the
    // write in the same stroke.
    //
    // Recording a grant is a two-step STEP-UP (issue #7770): POST only ARMS a
    // request (writing a host-only nonce the browser never sees), and the grant is
    // recorded by `kirocrew file-delivery approve` on the machine. That closes the
    // hole where an owner-authenticated but agent-DRIVEN browser could self-grant.
    //
    // The class travels in the query string, not a body, because that is what the
    // handler reads (`request.query.get("destination_class")`) on the writes.
    fileDeliveryConsent: () =>
      fetch(relocateRequestUrl('/api/file-delivery/consent')).then(j) as Promise<FileDeliveryConsentStatus>,
    armFileDeliveryConsent: (destinationClass: string) =>
      post('/api/file-delivery/consent?destination_class=' + encodeURIComponent(destinationClass))
        .then(j) as Promise<ArmedFileDeliveryConsent>,
    fileDeliveryConsentArmStatus: () =>
      fetch(relocateRequestUrl('/api/file-delivery/consent/arm')).then(j) as Promise<ArmedFileDeliveryConsent>,
    revokeFileDeliveryConsent: (destinationClass: string) =>
      del('/api/file-delivery/consent?destination_class=' + encodeURIComponent(destinationClass))
        .then(j) as Promise<{ ok?: boolean; removed?: boolean }>,
    // Credential-redaction switch (Settings > Security > Credential redaction).
    // Two explicit verbs for the same reason the consent helpers above keep
    // theirs: the handler applies the owner gate to the read and the write
    // separately, and the write is the ONLY writer of the keystone.
    credentialRedaction: () =>
      fetch(relocateRequestUrl('/api/security/credential-redaction')).then(j) as Promise<CredentialRedactionState>,
    setCredentialRedaction: (enabled: boolean) =>
      put('/api/security/credential-redaction', { enabled }).then(j) as Promise<CredentialRedactionState>,
  }

  return { posture, policies, secrets, consents }
}
