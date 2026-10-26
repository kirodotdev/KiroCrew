/**
 * The MCP management page's data model: what the gateway status and launch-preview
 * routes carry, the error-code-to-copy tables, the ONE derivation of a row's state and
 * the evidence verdict that flags a server shared against its assessment, plus the
 * wait the bulk action runs on the measurement pass. Tables and pure functions, except
 * `waitForMeasurePass`, which polls the pass and writes each reading into the query
 * cache; the page and its views import from here.
 */
import type { QueryClient } from '@tanstack/react-query'
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
  // Stubbed servers whose launch the gateway refused to run outside the session
  // sandbox, keyed by name. Optional: an older gateway does not send it.
  launch_refused?: Record<string, LaunchRefusal>
  // Share-list names, as configured, whose launch is refused, with the reason.
  // Matched by the gateway, which knows the alias a refusal is recorded under.
  share_refused?: Record<string, string>
}

/** Why a stubbed server's launch waits for the operator, and what approving it runs. */
export type LaunchRefusal = {
  reason: 'added_outside_dashboard' | 'changed_needs_reapproval'
  // Every launch the approve button covers, one per launch the server resolves to.
  commands?: string[][]
  // The declared environment of each launch, index-aligned with `commands`.
  envs?: string[][]
  // What the operator approved LAST time, when the approval record kept it. A
  // record written without it is readable as "not recorded", never as "empty":
  // an absent before-state and an approval of nothing are different facts, and
  // reading the second as the first would invent a comparison.
  approved_commands?: string[][]
  approved_envs?: string[][]
  // False when the launch could not be read in full, which is also when
  // `expected_launch` is absent: a partial launch cannot be approved.
  complete?: boolean
  // Present only when every launch above resolved in full; approval needs it.
  expected_launch?: string
}

/** What the gateway would run for one server, read before anything is written. */
export type LaunchPreview = {
  name: string
  commands: string[][]
  envs: string[][]
  complete: boolean
  expected_launch?: string
}

/** One server in the bulk review, retaining any read failure for its error surface. */
export type BulkCandidate = { name: string; preview?: LaunchPreview; error?: unknown }

export type BulkIssue = {
  name: string
  error: unknown
  kind: 'changed' | 'preview_failed' | 'write_failed'
}

/**
 * True when the launch read is the WHOLE launch, so an approval can name it.
 *
 * Both halves are load bearing. `expected_launch` is the identity the write is
 * checked against, and `complete: false` says the launch was cut short — approving
 * then would pin the approval to only part of what the server resolves, which is
 * the one thing this whole view exists to prevent.
 */
export function launchIsApprovable(launch: { complete?: boolean; expected_launch?: string }): boolean {
  return launch.complete !== false && !!launch.expected_launch
}

export const REFUSAL_REASON_KEY: Record<LaunchRefusal['reason'], string> = {
  added_outside_dashboard: 'pages.mcpManagement.reapproval_reason_added_outside_dashboard',
  changed_needs_reapproval: 'pages.mcpManagement.reapproval_reason_changed',
}

/**
 * The state pill's label. A refused launch has two causes, and the pill names
 * which one, so a changed command is not read as a server never approved.
 */
export function stateLabelKey(state: McpRowState, refusal?: LaunchRefusal): string {
  if (state === 'needs_reapproval' && refusal?.reason === 'changed_needs_reapproval') {
    return 'pages.mcpManagement.state_command_changed'
  }
  return STATE_LABEL_KEY[state]
}

export function stubErrorKey(error: unknown): string {
  if (!(error instanceof ApiError)) return 'pages.mcpManagement.stub_failed'
  let code: unknown
  try {
    code = JSON.parse(error.body)?.code
  } catch {
    return 'pages.mcpManagement.stub_failed'
  }
  if (error.status === 409 && code === 'launch_changed_since_display') {
    return 'pages.mcpManagement.stub_error_launch_changed'
  }
  if (error.status === 409 && code === 'launch_unresolved') {
    return 'pages.mcpManagement.stub_error_launch_unresolved'
  }
  if (error.status === 409 && code === 'launch_over_cap') {
    return 'pages.mcpManagement.stub_error_launch_over_cap'
  }
  if (error.status === 503 && code === 'approval_write_failed') {
    return 'pages.mcpManagement.stub_error_approval_write_failed'
  }
  // Sending stub=true without the identity of a launch that was read.
  if (error.status === 409 && code === 'expected_launch_required') {
    return 'pages.mcpManagement.stub_error_expected_launch_required'
  }
  if (error.status === 409 && code === 'launch_display_incomplete') {
    return 'pages.mcpManagement.stub_error_launch_display_incomplete'
  }
  // A batch cannot approve: one body carries no launch identity per name.
  if (error.status === 400 && code === 'batch_stub_requires_individual') {
    return 'pages.mcpManagement.stub_error_batch_stub_requires_individual'
  }
  return 'pages.mcpManagement.stub_failed'
}

/** True when the write was refused because the launch moved after it was read. */
export function launchChangedSinceDisplay(error: unknown): boolean {
  return stubErrorKey(error) === 'pages.mcpManagement.stub_error_launch_changed'
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

export function launchPreviewErrorKey(error: unknown): string {
  const fallback = 'pages.mcpManagement.launch_preview_error_failed'
  if (!(error instanceof ApiError)) return fallback
  let code: unknown
  try {
    code = JSON.parse(error.body)?.code
  } catch {
    return fallback
  }
  return (typeof code === 'string' && LAUNCH_PREVIEW_ERROR_KEY[code]) || fallback
}

/** The two sub-views: the decisions, and the evidence behind them. */
export type McpView = 'servers' | 'assessment'

/**
 * Catalog KEYS, not prose. The strict i18n lint reads inside ALL-CAPS module
 * constants, and a table of English labels here would both trip it and freeze the
 * copy in code; the key is resolved at render instead.
 *
 * Every tier the verdict engine can emit is present. An unrecognised tier — an
 * older or newer gateway naming one we do not know — falls back to "not
 * measured", which is the honest reading of a verdict we cannot interpret.
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
 * The states a row can be in, as a closed set.
 *
 * Deliberately NOT the i18n keys. Every reader other than the label itself asks
 * "is this row shared" or "was it declined", and comparing against a catalog key
 * makes that question depend on a string that lives in 13 JSON files: rename the
 * key and `!== 'pages.mcpManagement.state_shared'` quietly becomes "never shared",
 * which silently drops the accent colour, the sharing-without-support warning and
 * the assessment count with nothing for `tsc` to catch. This PR renamed one of
 * these keys once already. A discriminant makes the same mistake a type error.
 */
type McpRowState = 'no_stub' | 'needs_reapproval' | 'direct_env' | 'shared' | 'stub' | 'direct'

const STATE_LABEL_KEY: Record<McpRowState, string> = {
  no_stub: 'pages.mcpManagement.state_no_stub',
  needs_reapproval: 'pages.mcpManagement.state_needs_reapproval',
  direct_env: 'pages.mcpManagement.state_direct_env',
  shared: 'pages.mcpManagement.state_shared',
  stub: 'pages.mcpManagement.state_stub',
  direct: 'pages.mcpManagement.state_direct',
}

/**
 * What the server is running as, as ONE function used by both sub-views.
 *
 * The assessment table has to name the current state to be able to show it
 * disagreeing with the verdict, and two copies of this mapping would be free to
 * drift into saying different things about the same row.
 *
 * This is the SOLE derivation of a row's state. The chip's text, the chip's
 * colour, the per-row note and the sharing-without-support warning all read its
 * answer instead of recomputing their own, because a second spelling of any of
 * these states is the defect this change exists to remove: one copy gets corrected
 * and the other keeps saying `shared`.
 */
export function rowState(
  s: McpManagedServer,
  sharingOn: boolean,
  refused?: Record<string, LaunchRefusal>,
): McpRowState {
  if (!s.can_stub) return 'no_stub'
  // Outranks every other stubbed state: the gateway will not run this launch
  // outside the sandbox until the operator approves its command, so the session
  // launches the server itself whatever the other fields say.
  if (s.stub && refused?.[s.name]) return 'needs_reapproval'
  // The rewriter's decision outranks allowlist membership and the global switch,
  // but only for a row the operator opted IN, because that is the only row whose
  // state would otherwise be reported as shared.
  //
  // The state is `direct`, not a fourth thing: on this branch the rewriter passes
  // the ORIGINAL spec through and never reaches `_build_stub_entry`, so no stub is
  // created and the session launches the server itself -- which is what `direct`
  // means everywhere else on this page. `(env)` marks WHY an opted-in server ended
  // up there, and is the only part that is new.
  //
  // Scoped to the ENV obstacle on purpose: the rewriter also declines when it
  // cannot resolve the command, and the row payload carries no signal for that, so
  // such a row still reads `shared`. Naming it here would be a claim this data
  // cannot support -- it belongs with the backend-computed-state follow-up.
  //
  // A row the operator did NOT opt in reads plain `direct`: there the field is
  // forward-looking ("stubbing this would still not pool it"), which the batch
  // action uses as a skip reason, and nothing has been declined yet to explain.
  if (s.stub && s.pooling_blocked_by_env === true) return 'direct_env'
  if (s.stub && sharingOn) return 'shared'
  if (s.stub) return 'stub'
  return 'direct'
}

/** Evidence tiers that argue AGAINST sharing, as opposed to merely not endorsing it.
 *
 *  `refuted` is an observation of the server misbehaving while shared, and
 *  `disqualified` is a declaration we trust ruling sharing out up front. Those are
 *  the two that say something is wrong.
 */
export const CONTRARY_STRENGTHS = new Set(['refuted', 'disqualified'])

/**
 * True when the server is sharing a backend right now and the evidence argues
 * against it.
 *
 * This is the one thing on the page worth colouring, so what trips it has to be
 * evidence CONTRARY to sharing, never the mere absence of an endorsement. The two
 * are easy to conflate because both leave `recommendShare` false, and conflating
 * them makes the warning useless in opposite directions:
 *
 *   - `unknown` means nobody has measured this server. Flagging it would assert a
 *     finding from a measurement that never ran.
 *   - `no_objection` means nothing disqualifying was found. It is the weakest
 *     useful verdict and the one most servers sit at, so flagging it would put a
 *     permanent warning over a healthy fleet and teach the operator to ignore the
 *     only coloured signal on the page.
 *
 * Both of those are quiet. What speaks is `refuted` or `disqualified`.
 *
 * A row the rewriter declined to stub is not sharing at all, so it cannot be
 * sharing-without-support however damning its evidence is. That exclusion is not
 * spelled here: this asks `rowState` whether the row's state IS `shared`,
 * because a second spelling of "is sharing right now" is the same mistake as the
 * colour that used to disagree with the label -- one copy gets a new state added
 * to it and the other does not. Reading the label fixes both readers at once: the
 * warning icon on the row and the count the assessment view sends the operator
 * over to find.
 */
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
 * Poll the measurement pass until it stops. True when it finished in time.
 *
 * The pass measures every unmeasured server with no budget, two spawns each, so
 * on a fresh install it is minutes rather than seconds. Returning false rather
 * than proceeding is deliberate: acting on a half-measured fleet would stub
 * whatever happened to be done and silently skip the rest, which is exactly the
 * "reported more than it did" failure this control has to avoid.
 *
 * Each reading is written into the SAME query cache entry `MeasureControl` reads,
 * so the page has one source of displayed progress rather than two. Consuming the
 * readings here and throwing them away left the button's own line rendering a
 * hardcoded zero — a counter frozen at "0 of N" for up to four minutes, which
 * reads as a stalled pass on exactly the fresh install this control exists for.
 */
export async function waitForMeasurePass(qc: QueryClient): Promise<boolean> {
  const deadline = Date.now() + MEASURE_WAIT_MS
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, MEASURE_POLL_MS))
    // A failed read is not a finished pass. Treat it as "keep waiting" so a
    // single dropped request cannot make the caller act early; the deadline is
    // what ends the loop.
    try {
      const p = await api.mcpMeasureProgress()
      qc.setQueryData(['mcp-measure-progress'], p)
      if (!p.running) return true
    } catch {
      // keep polling
    }
  }
  return false
}
