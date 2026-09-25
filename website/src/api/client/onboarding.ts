/**
 * First-run readiness: the kiro-cli prerequisite probe and its repairs, the
 * KAS in-product Kiro sign-in, and the agent import from other tools.
 */

import type { ClientTransport } from './transport'

export interface KiroPrerequisiteStatus {
  platform: string
  installed: boolean
  authenticated: boolean
  ready: boolean
  initial_setup_complete: boolean
  repair_required: boolean
  docs_url: string
  /**
   * The command the USER runs to sign in (`kiro-cli login`). Supplied by the
   * gateway and rendered verbatim in a `<code>` — never a catalog value, because
   * a translated command cannot be typed.
   */
  login_command: string
  sso_login_command: string
  /**
   * True when the resolved CLI is the copy built into the desktop app. The gate
   * then explains why `login_command` is an absolute path into the app's own
   * resources rather than the bare name the user's shell would resolve.
   */
  bundled_cli: boolean
  setup_allowed: boolean
  /**
   * True when the CLI binary is present and executable but could not be
   * VERIFIED because this host cannot build a sandbox (verification runs the
   * binary inside it). A categorically different condition from a missing
   * binary — a failed sandbox build carries no information about whether the
   * CLI is installed.
   */
  sandbox_unavailable: boolean
  /** Crew OS sandbox capability; absent on older gateways, so bypass requires true. */
  sandbox_backend_available?: boolean
  /**
   * Whether this host permits execution with NO OS sandbox backend — the
   * platform default (native Windows) or an operator opt-in. A non-enforced
   * harness (not in `sandbox_blocked_backends`) needs no Crew OS mask, so it can
   * start and finish setup on such a host even when `sandbox_backend_available`
   * is false. Absent on older gateways; a consumer must FAIL CLOSED on the
   * absence (undefined ⇒ not permitted), never treat it as true.
   */
  unsandboxed_exec_permitted?: boolean
  /** Runtime-enforced harness ids whose credential mask cannot apply at the effective tier. */
  sandbox_blocked_backends?: string[]
  /** Machine-readable: 'transient' | 'foreign_sandbox' | 'no_backend' | ''. */
  sandbox_failure_kind: string
  /** Technical probe reason, e.g. 'unshare(CLONE_NEWNS) failed with errno 1 (EPERM)'. */
  sandbox_detail: string
  /**
   * Machine-readable host mechanism behind a Linux user-namespace denial:
   * 'apparmor_userns' | 'max_user_namespaces' | 'no_user_ns' | 'userns_denied' | ''.
   * Selects which concrete remedy the gate renders — the errno alone leaves the
   * user with nothing to act on.
   */
  sandbox_remedy: string
  /**
   * Kiro Crew's own agent spec files missing from the kiro-cli agents directory.
   * Non-empty means kiro-cli will answer every session/set_mode with
   * "Mode '<name>' not found", so `ready` is forced false and `repair_required`
   * true — a viable binary and a good `whoami` are NOT sufficient on their own.
   */
  missing_agent_specs: string[]
  /**
   * Why the last version probe did not verify the CLI when neither typed
   * condition above (sandbox refusal, timeout) explains it — the probe's own
   * failure text, or the tail of its output on a non-zero exit. Empty when the
   * probe passed, never ran, or a typed field already carries the cause. Shown
   * verbatim, untranslated, in the retry screen so it names WHY instead of just
   * that the check failed.
   */
  probe_error?: string
  /** The failed probe's exit status; absent when it did not exit. */
  probe_status?: number | null
  /**
   * Failure text from the repair the Check again button attempts when specs are
   * missing. Empty when none was attempted or it succeeded. Shown verbatim and
   * untranslated: it names the failing install step.
   */
  agent_spec_repair_error: string
  /**
   * Kiro Crew's own specs that are PRESENT on disk but which the installed
   * kiro-cli refuses to load. Presence and acceptance are different questions: a
   * rejected spec is dropped from kiro-cli's agent table, so `--agent kirocrew`
   * resolves to the default agent with none of Kiro Crew's MCP servers — the
   * same total failure as an absent spec, which statting the file cannot detect.
   * Non-empty forces `ready` false and `repair_required` true.
   *
   * Optional because a gateway older than this field does not send it.
   */
  rejected_agent_specs?: string[]
  /**
   * kiro-cli's own reason for the first rejection above, sanitized. Shown
   * verbatim and untranslated: it names the file and the construct refused.
   */
  agent_spec_rejection_detail?: string
  /**
   * Whether the installed kiro-cli exposes the `acp` subcommand every Kiro Crew
   * session is launched through. False means the CLI runs and is signed in but
   * is too OLD to start a session, so `ready` is forced false. The remedy is an
   * update, not a reinstall. Optional because a gateway older than this field
   * does not send it — treat a missing value as `true` (supported).
   */
  acp_supported?: boolean
  /**
   * The command that updates the CLI in place (`kiro-cli update`). Unlike
   * `login_command`, Kiro Crew also runs this FOR the owner via the update-cli
   * POST — it is the CLI's own self-update. Rendered verbatim in a `<code>`.
   */
  update_command?: string
  /**
   * Failure text from an update-cli attempt. Empty when none was attempted or it
   * succeeded. Shown verbatim and untranslated: it names why the self-update did
   * not complete.
   */
  cli_update_error?: string
}

export interface KasLoginStatus {
  authenticated: boolean
  /** Provider of the active sign-in as the token records it ('Google', 'Github', 'BuilderId', 'Enterprise'), '' when signed out. */
  provider: string | null
  /** Which vault slot the sign-in occupies ('social' | 'builder_id' | 'identity_center' | 'external_idp'); the value `kasLoginLogout` takes. '' when signed out. */
  identity: string | null
  /**
   * How a sign-in can return to this gateway. 'loopback' means the browser and
   * the gateway share a machine, so the OAuth callback lands directly on a
   * local port; 'device' means the gateway is remote and the user instead
   * approves a short code in their own browser (no callback required).
   */
  transport: 'loopback' | 'device'
  /** ISO-8601 UTC instant the stored access token stops working; null when signed out. */
  expires_at: string | null
  /** True when the access token is at or inside the engine's refresh margin. */
  expired: boolean
  /** True when a refresh token is stored to renew the access token with. */
  has_refresh_token: boolean
  /**
   * True when the issuer refused the last refresh: the sign-in looks renewable
   * but is not, and only signing in again fixes it. Cleared by any new
   * credential landing in the slot.
   */
  refresh_rejected: boolean
  /**
   * The spawn-time verdict: can this identity still answer an agent's
   * credential request without a sign-in? Same predicate `kirocrew doctor`
   * prints. Independent of `refresh_rejected` on purpose: a rejected refresh
   * is reported to the user, never used to hand the agent back to kiro-cli's
   * login behind their back.
   */
  usable: boolean
}

export interface KasLoginDeviceSession {
  /** Handle for polling this sign-in attempt. */
  login_id: string
  /** The short code the user types into the verification page. */
  user_code: string
  /** The page (opened on ANY device) where the code is entered. */
  verification_uri_complete: string
  /** ISO-8601 UTC instant the code stops working. */
  expires_at: string
}

export interface KasLoginPollResult {
  status: 'pending' | 'authorized' | 'expired' | 'error'
  /**
   * Machine-readable failure code — error responses carry one too. On an
   * `authorized` answer it can be `previous_identity_not_removed`: the new
   * credential landed but the slot named by `replaces` could not be deleted,
   * so the previous account still takes precedence until it is signed out.
   */
  code?: string
  error?: string
  /** On `authorized` after a begin with `replaces`: every other stored slot the
   *  switch removed so the new account is the one the store resolves to. */
  replaced?: string[]
}

/**
 * A loopback (PKCE) sign-in the gateway is listening for on a local port. The
 * dashboard opens `auth_url` in the user's browser; the portal redirects back
 * to `http://localhost:<port>` on this machine, and the gateway finishes the
 * exchange itself — the user confirms nothing. Polled with the same login_id.
 */
export interface KasLoginLoopbackSession extends KasLoginDeviceSession {
  auth_url: string
  port: number
}

export interface AgentImportCategory {
  id: string
  label: string
  count: number
  description?: string
}

export interface AgentImportSource {
  id: string
  name: string
  detected: boolean
  detail?: string
  categories: AgentImportCategory[]
}

export interface AgentImportSkipped {
  source: string
  category: string
  reason: string
  count?: number
}

export interface AgentImportScanResponse {
  sources: AgentImportSource[]
  skipped?: AgentImportSkipped[]
  merge_only: true
}

export interface AgentImportSelection {
  id: string
  categories: string[]
}

/** Skip keeps Kiro Crew's item; rename installs alongside; overwrite replaces it
 *  after the backend writes a restore copy. Omitting the field means 'skip'. */
export type AgentImportConflictStrategy = 'skip' | 'rename' | 'overwrite'

export interface AgentImportApplyRequest {
  sources: AgentImportSelection[]
  conflict_strategy?: AgentImportConflictStrategy
}

export interface AgentImportSummary {
  imported: number
  deduplicated: number
  skipped: number
  conflicts: number
  /** How many of `conflicts` a retry with rename/overwrite could clear. */
  resolvable_conflicts: number
}

export interface AgentImportApplyResponse {
  ok: true
  conflict_strategy: AgentImportConflictStrategy
  summary: AgentImportSummary
}

export function createOnboardingEndpoints({ get, post, put, j, jNullable }: ClientTransport) {
  const readiness = {
    // Background polls read the gateway's latched state (no kiro-cli subprocess).
    // `refresh` is the explicit user action (Refresh / Check again) that forces a
    // real host probe.
    /**
     * `refresh` picks the probe mode, and the two are deliberately different:
     * `'explicit'` is the human Check again and always probes the host, `'auto'` is
     * the blocking gate's poll and is coalesced server-side behind a short floor so
     * several open tabs cannot multiply the `kiro-cli` spawns. `false` reads the
     * gateway's latched state and spawns nothing.
     */
    kiroPrerequisite: (refresh: false | 'auto' | 'explicit' = false) => {
      // Built with URLSearchParams, as `artifacts` in `./artifacts` is: the mode
      // is its own wire value, so there is no query-string literal for the i18n
      // gate to mistake for user-visible copy.
      const params = new URLSearchParams()
      if (refresh) params.set('refresh', refresh)
      const s = params.toString()
      return get(`/api/kiro-prerequisite${s ? `?${s}` : ''}`).then(
        j,
      ) as Promise<KiroPrerequisiteStatus>
    },
    // A POST, not a flag on the status GET: the gateway's CSRF check and its SEL
    // audit are both method-scoped, so a spec rewrite reached from a GET would be
    // cross-site triggerable and would leave no audit record.
    repairKiroPrerequisiteSpecs: () =>
      post('/api/kiro-prerequisite/repair-specs').then(j) as Promise<KiroPrerequisiteStatus>,
    // A POST for the same CSRF/audit reasons as the spec repair above. Runs the
    // CLI's own in-place self-update on the gateway host and returns the
    // post-update snapshot; `cli_update_error` is empty on success.
    updateKiroPrerequisiteCli: () =>
      post('/api/kiro-prerequisite/update-cli').then(j) as Promise<KiroPrerequisiteStatus>,
    // KAS-mode in-product sign-in (no kiro-cli, no terminal). Status is a cheap
    // read; every step that changes sign-in state is a POST for the same
    // CSRF/audit reasons as the spec repair above. Error responses carry a
    // machine-readable `code` field alongside the human message.
    kasLoginStatus: () => get('/api/kas-login').then(j) as Promise<KasLoginStatus>,
    // `replaces` names the vault slot (`KasLoginStatus.identity`) a signed-in
    // user is switching away from; the gateway removes it only once THIS login's
    // credential has landed, so a failed switch leaves the old account intact
    // and a successful one cannot leave it shadowing the new account (the store
    // resolves by slot priority, not recency).
    kasLoginBeginDevice: (
      provider: string,
      extra?: { start_url?: string; region?: string; replaces?: string },
    ) =>
      post('/api/kas-login/device', { provider, ...(extra ?? {}) }).then(
        j,
      ) as Promise<KasLoginDeviceSession>,
    kasLoginPoll: (login_id: string) =>
      post('/api/kas-login/poll', { login_id }).then(j) as Promise<KasLoginPollResult>,
    // Loopback begin answers 409 `loopback_unavailable` when this install shape
    // cannot receive the callback (or every allowlisted port is busy); the gate
    // treats that as "start the device flow instead", not as a failure.
    kasLoginBeginLoopback: (provider: string, extra?: { replaces?: string }) =>
      post('/api/kas-login/loopback', { provider, ...(extra ?? {}) }).then(
        j,
      ) as Promise<KasLoginLoopbackSession>,
    // Idempotent: releases a loopback listener's port early on every start-over path.
    kasLoginCancel: (login_id: string) =>
      post('/api/kas-login/cancel', { login_id }).then(j) as Promise<{ ok: boolean }>,
    // Signs out of Crew's Kiro identity: deletes the named slot
    // (`KasLoginStatus.identity`, the one the card shows) AND every other stored
    // slot, so no lower-priority account can quietly take over, then recycles
    // running agent processes. Answers `{ok}`; the card re-reads status
    // afterwards, which is the single authority on state.
    kasLoginLogout: (identity: string) =>
      post('/api/kas-login/logout', { identity }).then(j) as Promise<{ ok: boolean }>,
    onboardingImportScan: () =>
      get('/api/onboarding/import/scan').then(j) as Promise<AgentImportScanResponse>,
    onboardingImportApply: (body: AgentImportApplyRequest) =>
      post('/api/onboarding/import/apply', body).then(j) as Promise<AgentImportApplyResponse>,
    onboardingImportState: (body: { completed: true }) =>
      put('/api/onboarding/import/state', body).then(jNullable) as Promise<{ ok?: boolean } | null>,
  }

  return { readiness }
}
