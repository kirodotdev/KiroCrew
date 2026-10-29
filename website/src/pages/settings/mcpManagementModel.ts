/**
 * The MCP management page's data model: what the gateway status and launch-preview
 * routes carry, the error-code-to-copy tables, the ONE derivation of a row's state and
 * the evidence verdict that flags a server shared against its assessment, plus the
 * wait the Share all action runs on the measurement pass. Tables and pure functions,
 * except `waitForMeasurePass`, which polls the pass and writes each reading into the
 * query cache; the page and its views import from here.
 */
import { type QueryClient } from '@tanstack/react-query'
import {
  api,
  ApiError,
  type McpManagedServer,
} from '../../api/client'

export const DOCS_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/architecture/design-notes/mcp-stub-decoupling.md'

export type GatewayStatus = {
  enabled: boolean
  stub: string[]
  stub_count: number
  running: boolean
  ping_ok: boolean
  supported: boolean
  // Stubbed servers whose launch the gateway has not recorded yet, keyed by
  // name. Optional: an older gateway does not send it.
  launch_refused?: Record<string, LaunchRefusal>
  // Share-list names, as configured, whose launch is refused, with the reason.
  // Matched by the gateway, which knows the alias a refusal is recorded under.
  share_refused?: Record<string, string>
}

/**
 * A shared server whose current launch is not recorded yet; it waits for a confirm.
 * `complete` and `expected_launch` pin the confirm. The row's Details shows the
 * display-safe launch (and the approved one before a change): commands and
 * args as the gateway bounded them, env KEY names only.
 */
export type LaunchRefusal = {
  reason: 'added_outside_dashboard' | 'changed_needs_reapproval'
  commands?: string[][]
  envs?: string[][]
  approved_commands?: string[][]
  approved_envs?: string[][]
  // False when the launch could not be read in full, which is also when
  // `expected_launch` is absent: a partial launch cannot be approved.
  complete?: boolean
  // Present only when every launch resolved in full; approval needs it.
  expected_launch?: string
}

/** What the gateway would run for one server, read at the press, before anything is written. */
export type LaunchPreview = {
  name: string
  commands: string[][]
  envs: string[][]
  complete: boolean
  expected_launch?: string
}

export type BulkCandidate = { name: string; preview?: LaunchPreview; error?: unknown }

/**
 * True when the launch read is the WHOLE launch, so an approval can name it.
 *
 * Both halves are load bearing. `expected_launch` is the identity the write is
 * checked against, and `complete: false` says the launch was cut short: approving
 * then would pin the approval to only part of what the server resolves.
 */
export function launchIsApprovable(launch: { complete?: boolean; expected_launch?: string }): boolean {
  return launch.complete !== false && !!launch.expected_launch
}


function errorCode(error: unknown): unknown {
  if (!(error instanceof ApiError)) return undefined
  try {
    return JSON.parse(error.body)?.code
  } catch {
    return undefined
  }
}

export function stubErrorKey(error: unknown): string {
  const code = errorCode(error)
  if (code === 'launch_changed_since_display') return 'pages.mcpManagement.stub_error_launch_changed'
  // Sending stub=true without the identity of a launch that was read.
  if (code === 'expected_launch_required') return 'pages.mcpManagement.stub_error_expected_launch_required'
  if (code === 'launch_display_incomplete') return 'pages.mcpManagement.stub_error_launch_display_incomplete'
  // A batch cannot approve: one body carries no launch identity per name.
  if (code === 'batch_stub_requires_individual') {
    return 'pages.mcpManagement.stub_error_batch_stub_requires_individual'
  }
  if (code === 'launch_unresolved') return 'pages.mcpManagement.stub_error_launch_unresolved'
  if (code === 'launch_over_cap') return 'pages.mcpManagement.stub_error_launch_over_cap'
  if (code === 'approval_write_failed') return 'pages.mcpManagement.stub_error_approval_write_failed'
  return 'pages.mcpManagement.stub_failed'
}

/**
 * What kind of change paused a shared server, as its hint key: its command
 * (which carries its version) or only its settings. Read from the refusal's own
 * before and after, which the row's Details also shows; with no recorded
 * before, the general line.
 */
const CHANGED_HINT_KEY = {
  any: 'pages.mcpManagement.command_changed_hint',
  command: 'pages.mcpManagement.command_changed_hint_command',
  settings: 'pages.mcpManagement.command_changed_hint_settings',
} as const

export function changedHintKey(refusal?: LaunchRefusal): string {
  const keys = CHANGED_HINT_KEY
  const same = (a?: string[][], b?: string[][]) => JSON.stringify(a ?? []) === JSON.stringify(b ?? [])
  if (refusal?.approved_commands === undefined) return keys.any
  if (!same(refusal.commands, refusal.approved_commands)) return keys.command
  if (!same(refusal.envs, refusal.approved_envs)) return keys.settings
  return keys.any
}

/**
 * One message per code the launch READ can fail with.
 *
 * Separate from the write's table on purpose: these all end in "nothing was turned
 * on", because the read happens before any write is attempted, and reusing the
 * write's wording would tell the operator a save was rolled back that never ran.
 */
const LAUNCH_PREVIEW_ERROR_KEY: Record<string, string> = {
  name_required: 'pages.mcpManagement.launch_preview_error_name_required',
  invalid_server_name: 'pages.mcpManagement.launch_preview_error_invalid_name',
  launch_unresolved: 'pages.mcpManagement.launch_preview_error_unresolved',
  launch_over_cap: 'pages.mcpManagement.launch_preview_error_over_cap',
  launch_resolve_failed: 'pages.mcpManagement.launch_preview_error_failed',
}

export type LaunchPreviewFailure = { code: string }

function isLaunchPreviewFailure(error: unknown): error is LaunchPreviewFailure {
  if (typeof error !== 'object' || error === null || error instanceof ApiError) return false
  return typeof (error as { code?: unknown }).code === 'string'
}

export function launchPreviewErrorKey(error: unknown): string {
  const fallback = 'pages.mcpManagement.launch_preview_error_failed'
  if (isLaunchPreviewFailure(error)) return LAUNCH_PREVIEW_ERROR_KEY[error.code] || fallback
  const code = errorCode(error)
  return (typeof code === 'string' && LAUNCH_PREVIEW_ERROR_KEY[code]) || fallback
}

const LAUNCH_PREVIEW_BATCH = 200

export async function readLaunchPreviews(names: string[]): Promise<BulkCandidate[]> {
  const chunks: string[][] = []
  for (let i = 0; i < names.length; i += LAUNCH_PREVIEW_BATCH) {
    chunks.push(names.slice(i, i + LAUNCH_PREVIEW_BATCH))
  }
  const results: BulkCandidate[] = []
  for (const chunk of chunks) {
    try {
      const { launches, errors } = await api.mcpGatewayLaunchPreviews(chunk)
      for (const name of chunk) {
        const preview = launches && Object.hasOwn(launches, name) ? launches[name] : undefined
        const code = errors && Object.hasOwn(errors, name) ? errors[name] : undefined
        if (preview) results.push({ name, preview })
        else results.push({ name, error: { code: code ?? 'launch_resolve_failed' } })
      }
    } catch (error) {
      for (const name of chunk) results.push({ name, error })
    }
  }
  return results
}

/** The two sub-views: the switches, and the evidence behind them. */
export type McpView = 'servers' | 'assessment'

/**
 * Catalog KEYS, not prose: the strict i18n lint reads inside ALL-CAPS module
 * constants. An unrecognised tier falls back to "not measured".
 */
export const STRENGTH_LABEL_KEY: Record<string, string> = {
  refuted: 'pages.mcpManagement.assessment.strength_refuted',
  disqualified: 'pages.mcpManagement.assessment.strength_disqualified',
  declared: 'pages.mcpManagement.assessment.strength_declared',
  measured: 'pages.mcpManagement.assessment.strength_measured',
  no_objection: 'pages.mcpManagement.assessment.strength_no_objection',
  unknown: 'pages.mcpManagement.assessment.strength_unknown',
}

/** One key per reason code the verdict engine emits. */
export const REASON_LABEL_KEY: Record<string, string> = {
  observed_hazard: 'pages.mcpManagement.assessment.reason_observed_hazard',
  not_stdio: 'pages.mcpManagement.assessment.reason_not_stdio',
  session_bound_by_construction:
    'pages.mcpManagement.assessment.reason_session_bound_by_construction',
  rotating_secret_env: 'pages.mcpManagement.assessment.reason_rotating_secret_env',
  not_probed: 'pages.mcpManagement.assessment.reason_not_probed',
  degrades_when_shared: 'pages.mcpManagement.assessment.reason_degrades_when_shared',
  handshake_not_reproducible:
    'pages.mcpManagement.assessment.reason_handshake_not_reproducible',
  declares_caller_identity: 'pages.mcpManagement.assessment.reason_declares_caller_identity',
  all_tools_read_only: 'pages.mcpManagement.assessment.reason_all_tools_read_only',
  preflight_passed: 'pages.mcpManagement.assessment.reason_preflight_passed',
  preflight_not_run: 'pages.mcpManagement.assessment.reason_preflight_not_run',
  no_objection_found: 'pages.mcpManagement.assessment.reason_no_objection_found',
  no_tool_annotations: 'pages.mcpManagement.assessment.reason_no_tool_annotations',
  no_tools_listed: 'pages.mcpManagement.assessment.reason_no_tools_listed',
}

/**
 * The states a row can be in, as a closed set -- deliberately not the i18n keys,
 * so "is this row shared" is a type check rather than a string compare against a
 * key that lives in 13 catalogs.
 */
export type McpRowState = 'no_stub' | 'needs_reapproval' | 'direct_env' | 'shared' | 'stub' | 'direct'

const STATE_LABEL_KEY: Record<McpRowState, string> = {
  no_stub: 'pages.mcpManagement.state_no_stub',
  needs_reapproval: 'pages.mcpManagement.state_needs_reapproval',
  direct_env: 'pages.mcpManagement.state_direct_env',
  shared: 'pages.mcpManagement.state_shared',
  stub: 'pages.mcpManagement.state_stub',
  direct: 'pages.mcpManagement.state_direct',
}

/**
 * What the server is running as, as ONE function used by both sub-views. The
 * chip, its colour and the assessment's warning all read this answer.
 */
export function rowState(
  s: McpManagedServer,
  sharingOn: boolean,
  refused?: Record<string, LaunchRefusal>,
): McpRowState {
  if (!s.can_stub) return 'no_stub'
  if (s.stub && refused?.[s.name]) return 'needs_reapproval'
  if (s.stub && s.pooling_blocked_by_env === true) return 'direct_env'
  // A server whose env names a vault secret is wrapped onto a backend of its
  // own: started by the gateway, but never shared. That is what `stub` means
  // here, so it must not read `shared` because the global switch is on.
  if (s.stub && s.private_backend === true) return 'stub'
  if (s.stub && sharingOn) return 'shared'
  if (s.stub) return 'stub'
  return 'direct'
}

/**
 * The state chip's label. A refused launch has two causes, and the chip names
 * which one: a server never approved here is not a changed command.
 */
export function stateLabelKey(state: McpRowState, refusal?: LaunchRefusal): string {
  // A refusal with no approvable identity has no way back to sharing here, so
  // its chip says it cannot be shared rather than naming a fixable change.
  if (state === 'needs_reapproval' && refusal && !launchIsApprovable(refusal)) {
    return 'pages.mcpManagement.state_no_stub'
  }
  if (state === 'needs_reapproval' && refusal?.reason === 'added_outside_dashboard') {
    return 'pages.mcpManagement.state_not_approved_yet'
  }
  return STATE_LABEL_KEY[state]
}

/**
 * The one line a row's Details gives for WHY it is in its state. Read only from
 * what the gateway sent: the row's own flags and, for a paused row, its refusal.
 * `can_stub` is "stdio AND not denylisted", so a row that cannot share and is
 * not denylisted is one with no stdio pipe.
 */
export function detailsWhyKey(
  s: McpManagedServer,
  state: McpRowState,
  refusal?: LaunchRefusal,
): string {
  switch (state) {
    case 'no_stub':
      return s.denylisted ? 'pages.mcpManagement.why_credentials' : 'pages.mcpManagement.why_no_stdio'
    case 'needs_reapproval':
      if (refusal && !launchIsApprovable(refusal)) return 'pages.mcpManagement.approval_not_approvable_here'
      if (refusal?.reason === 'added_outside_dashboard') {
        return 'pages.mcpManagement.reapproval_reason_added_outside_dashboard_unapprovable'
      }
      return changedHintKey(refusal)
    case 'direct_env':
      return 'pages.mcpManagement.state_direct_env_reason'
    case 'shared':
      return 'pages.mcpManagement.why_shared'
    case 'stub':
      // A private backend reads `stub` whatever the switch says, so the
      // "waiting for sharing" reason would be wrong for it.
      return s.private_backend === true
        ? 'pages.mcpManagement.why_private_backend'
        : 'pages.mcpManagement.why_stub_off'
    case 'direct':
      return 'pages.mcpManagement.why_direct'
  }
}

/**
 * The servers turning page-wide sharing ON also enables: shareable ones nobody
 * chose for. Never one the operator turned off (`declined`), never one that
 * cannot share (no stdio pipe, a credential vendor, an env a shared copy would
 * withhold), never one already marked, and never one waiting for an approval.
 * The endpoint's own eligibility check still decides each write.
 */
export function autoShareCandidates(
  servers: readonly McpManagedServer[],
  refused?: Record<string, LaunchRefusal>,
): McpManagedServer[] {
  return servers.filter(
    s =>
      s.can_stub &&
      !s.denylisted &&
      !s.stub &&
      s.declined !== true &&
      s.pooling_blocked_by_env !== true &&
      !refused?.[s.name],
  )
}

/** Marked servers whose launch waits for the operator's OK, and can get it here. */
export function waitingForApproval(
  servers: readonly McpManagedServer[],
  refused?: Record<string, LaunchRefusal>,
): McpManagedServer[] {
  return servers.filter(s => {
    const refusal = s.stub ? refused?.[s.name] : undefined
    return refusal !== undefined && launchIsApprovable(refusal)
  })
}

/** What reloading a set of chats did. */
export type ReloadOutcome = { done: string[]; skipped: string[]; failed: string[] }

/**
 * Reload each chat in turn through the dashboard's own per-chat reload. A chat
 * the gateway refuses with 409 is busy (a turn running, an approval waiting,
 * sub-agents attached) and is SKIPPED, never retried or forced: that refusal
 * is the guard that keeps running work alive. Anything else is a failure.
 */
export async function reloadChats(
  slots: readonly string[],
  reload: (slot: string) => Promise<unknown>,
): Promise<ReloadOutcome> {
  const out: ReloadOutcome = { done: [], skipped: [], failed: [] }
  for (const slot of slots) {
    try {
      await reload(slot)
      out.done.push(slot)
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) out.skipped.push(slot)
      else out.failed.push(slot)
    }
  }
  return out
}

/**
 * What changed between a refusal's approved launch and its new one: the
 * arguments (as tokens) and the env key NAMES on each side. Order-insensitive
 * and per token, so a version bump inside one argument reads as that one
 * argument changed.
 */
export function launchDiff(refusal: LaunchRefusal): {
  removed: string[]
  added: string[]
  envAdded: string[]
  envRemoved: string[]
} {
  const before = (refusal.approved_commands ?? []).flat()
  const after = (refusal.commands ?? []).flat()
  const left = new Set(before)
  const right = new Set(after)
  const envBefore = new Set(envKeyNames(refusal.approved_envs))
  const envAfter = new Set(envKeyNames(refusal.envs))
  return {
    removed: [...new Set(before.filter(t => !right.has(t)))],
    added: [...new Set(after.filter(t => !left.has(t)))],
    envAdded: [...envAfter].filter(n => !envBefore.has(n)),
    envRemoved: [...envBefore].filter(n => !envAfter.has(n)),
  }
}

/** Bytes as GiB with one decimal, the number only: the unit is in the catalog. */
export function gibNumber(bytes: number): number {
  return Math.round((bytes / 1024 ** 3) * 10) / 10
}

/** One argv as a person would type it: an arg with a space or quote is quoted. */
export function argvLine(argv: readonly string[]): string {
  return argv.map(a => (a === '' || /[\s'"\\]/.test(a) ? JSON.stringify(a) : a)).join(' ')
}

/**
 * The env KEY names of a launch display, never a value. The gateway already
 * redacts values (`KEY=<hidden sha256:…>`); the page drops even that half.
 */
export function envKeyNames(envs?: readonly (readonly string[])[]): string[] {
  const names = new Set<string>()
  for (const env of envs ?? []) {
    for (const pair of env) {
      const key = pair.split('=', 1)[0]
      if (key) names.add(key)
    }
  }
  return [...names]
}

/** Evidence tiers that argue AGAINST sharing, as opposed to merely not endorsing it. */
export const CONTRARY_STRENGTHS = new Set(['refuted', 'disqualified'])

/** True when the server is shared right now and the evidence argues against it. */
export function sharedWithoutSupport(
  s: McpManagedServer,
  sharingOn: boolean,
  refused?: Record<string, LaunchRefusal>,
): boolean {
  if (rowState(s, sharingOn, refused) !== 'shared') return false
  const rec = s.recommendation
  if (!rec) return false
  return CONTRARY_STRENGTHS.has(rec.strength)
}

/** How long the batch action waits for an uncapped measurement pass, in ms. */
const MEASURE_WAIT_MS = 4 * 60 * 1000
const MEASURE_POLL_MS = 1500

/**
 * Poll the measurement pass until it stops. True when it finished in time. Each
 * reading lands in the SAME cache entry `MeasureControl` reads, so the page has
 * one source of displayed progress.
 */
export async function waitForMeasurePass(qc: QueryClient): Promise<boolean> {
  const deadline = Date.now() + MEASURE_WAIT_MS
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, MEASURE_POLL_MS))
    try {
      const p = await api.mcpMeasureProgress()
      qc.setQueryData(['mcp-measure-progress'], p)
      if (!p.running) return true
    } catch {
      // A failed read is not a finished pass; the deadline ends the loop.
    }
  }
  return false
}
