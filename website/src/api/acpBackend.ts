import { i18nT } from '../i18n/t'
import { ApiError } from './apiError'
import type { AcpBackendProbe } from './client/config'

/**
 * Which agent harness the gateway runs, read off `agent.acp_backend` in the
 * `/api/config/kirocrew` body (the `['kirocrewConfig']` query).
 *
 * Stated POSITIVELY, mirroring the gateway's `is_kiro_backend`: a call site that
 * means "kiro" says so, rather than inferring it from "not claude" — an inference
 * that would silently hand Kiro-only behaviour to every harness added later.
 */

/**
 * The kiro-cli backend's id in the config vocabulary the dashboard reads and
 * writes (`STORED_BACKEND_KIRO` on the gateway). The config body, the schema
 * enum and the `/api/acp-backends` rows all spell Kiro CLI `'kiro'`; an empty
 * or absent `agent.acp_backend` means "no choice", which is the default.
 */
export const ACP_BACKEND_KIRO = 'kiro'

/** The KAS backend's id (`ACP_BACKEND_KAS` on the gateway): kiro-cli's relay,
 *  which completes first-run setup on kiro-cli ACP support, not a kiro-cli login. */
export const ACP_BACKEND_KAS = 'kas'

/** kiro-cli and its KAS relay: the harnesses backed by the Kiro service. */
export const KIRO_HARNESS_BACKENDS: readonly string[] = [ACP_BACKEND_KIRO, ACP_BACKEND_KAS]

/** The harness an install runs when it holds no choice (absent or `''`). */
export const DEFAULT_ACP_BACKEND = ACP_BACKEND_KAS

/** The slice of the config body this check reads. */
export interface AcpBackendConfig {
  agent?: { acp_backend?: string }
}

/**
 * The harness a LOADED config runs: its `agent.acp_backend`, with absent and
 * `''` ("no choice") read as the default.
 */
export function configuredAcpBackend(cfg: AcpBackendConfig): string {
  return cfg.agent?.acp_backend || DEFAULT_ACP_BACKEND
}

/**
 * True only when a LOADED config names the kiro-cli backend. `undefined` (the
 * config has not loaded, or failed to) is not kiro: nothing Kiro-only should
 * render on a guess, and the pill's dash is exactly such a thing.
 */
export function isKiroBackend(cfg: AcpBackendConfig | undefined): boolean {
  if (cfg === undefined) return false
  return configuredAcpBackend(cfg) === ACP_BACKEND_KIRO
}

/**
 * True only when a LOADED config names a harness that bills in credits — the
 * unit the Usage tab's spend chart stacks. kiro-cli and its KAS relay both
 * report a turn's credits (`TurnUsage.credits` on the gateway); every other
 * harness fills token counts and a dollar cost and leaves credits at zero, so a
 * credits chart over its rows would show "no spend" for turns that did run.
 * Named positively, like `isKiroBackend`, so a harness added later is not
 * assumed to bill in credits.
 */
export function reportsCredits(cfg: AcpBackendConfig | undefined): boolean {
  if (cfg === undefined) return false
  return KIRO_HARNESS_BACKENDS.includes(configuredAcpBackend(cfg))
}

/**
 * True only when a LOADED config runs one of the Kiro harnesses, which apply
 * the user's app.kiro.dev model fallbacks inside the turn before any of Kiro
 * Crew's own. Separate from `reportsCredits` because the two answer different
 * questions that happen to name the same harnesses today.
 */
export function isKiroHarness(cfg: AcpBackendConfig | undefined): boolean {
  if (cfg === undefined) return false
  return KIRO_HARNESS_BACKENDS.includes(configuredAcpBackend(cfg))
}

/** Shared translated harness labels, one per harness the gateway knows
 *  (`ACP_BACKENDS_KNOWN`; `test_harness_names_cover_every_known_backend` holds
 *  the list). A harness an edition registers later keeps the server's policy name. */
export function acpBackendName(backend: { id: string; policy_id?: string }): string {
  switch (backend.id) {
    case ACP_BACKEND_KIRO: return i18nT('pages.developer.agentBackendTab.kiro_cli')
    case 'claude': return i18nT('pages.developer.agentBackendTab.claude_code')
    case ACP_BACKEND_KAS: return i18nT('pages.developer.agentBackendTab.kas_kiro_agent')
    case 'codex': return i18nT('pages.developer.agentBackendTab.codex')
    case 'copilot': return i18nT('pages.developer.agentBackendTab.copilot')
    case 'deepseek': return i18nT('pages.developer.agentBackendTab.deepseek')
    case 'goose': return i18nT('pages.developer.agentBackendTab.goose')
    case 'opencode': return i18nT('pages.developer.agentBackendTab.opencode')
    case 'pi': return i18nT('pages.developer.agentBackendTab.pi')
    case 'strands': return i18nT('pages.developer.agentBackendTab.strands')
    default: return backend.policy_id || backend.id
  }
}

/** A harness name for a row badge. Every built-in name is already short
 *  ("Kiro Agent", "Kiro CLI", "Claude Code"), so the badge reads the same
 *  translated label as everywhere else rather than a private abbreviation. */
export function acpBackendShortName(id: string): string {
  return acpBackendName({ id })
}

/**
 * What turning ONE MCP tool off costs on a harness, as the two sentences the
 * reader sees, from that harness's `GET /api/acp-backends` row.
 *
 * Settings → Agent Harness (the card for each harness) and Connections → MCP
 * Servers (the tool list, for the harness in use) both read it here, so the two
 * surfaces cannot say different things about the same harness (#11969).
 *
 * - `rule`: only where the server classed the reach as able to cost a whole
 *   server (`mcp.costs_whole_server === true`). The classification is the
 *   server's (each reach declares it on
 *   `providers/mirrors/registry.PerToolDeny.costs_whole_server`), never re-derived from the reach
 *   name here: today it is `whole-server` and `per-call`. Compared against
 *   `true`, so a missing flag (an older gateway) or a truthy string says nothing.
 * - `exception`: only on a `per-call` reach, which spares Crew's own servers.
 *
 * Each is `''` where it does not hold. No row (query in flight, 403, 404) is
 * both `''`: the absence of probe facts is not something to tell the reader.
 */
export function mcpWholeServerDeny(
  probe: AcpBackendProbe | undefined,
): { rule: string; exception: string } {
  if (!probe) return { rule: '', exception: '' }
  const name = acpBackendName(probe)
  return {
    rule: probe.mcp?.costs_whole_server === true
      ? i18nT('pages.developer.agentBackendTab.mcp_deny_rule', { name })
      : '',
    exception: probe.mcp?.per_tool_deny === 'per-call'
      ? i18nT('pages.developer.agentBackendTab.mcp_deny_exception', { name })
      : '',
  }
}

/**
 * The same cost, worded for ONE server's open tool list.
 *
 * The gateway lists the servers its reach spares (`mcp.spared_servers`, read from
 * the same `CONTROL_PLANE_SERVERS` tuple the codex and opencode mirrors use), so
 * the list says only the fact that holds there: the rule on every other server,
 * and "stops only that tool" on a spared one. The harness card keeps both
 * sentences, since it describes the harness rather than one server.
 *
 * A gateway that predates the field sends no list, and then this is the pair
 * `mcpWholeServerDeny` returns, as before: guessing which servers are spared
 * from a name here would drift from the server's tuple.
 */
export function mcpServerDeny(
  probe: AcpBackendProbe | undefined,
  server: string,
): { rule: string; exception: string } {
  const pair = mcpWholeServerDeny(probe)
  const spared = probe?.mcp?.spared_servers
  if (!probe || !Array.isArray(spared)) return pair
  if (!spared.includes(server)) return { rule: pair.rule, exception: '' }
  return {
    rule: '',
    exception: pair.rule
      ? i18nT('pages.overview.mcpTab.mcp_deny_spared', { name: acpBackendName(probe) })
      : '',
  }
}

/**
 * The parsed body of a 503 `setup_marker_write_failed`, or `null` for any other
 * error. Both the first-run gate and Settings → Agent Harness read it, so it
 * lives here rather than in either screen: the config PATCH and the backend
 * re-check answer it, the agent choice committed either way, and only the
 * durable first-run marker did not.
 *
 * `backend` rides along on a re-check: the re-probe succeeded and only the
 * marker write failed, so the caller can splice the fresh row in exactly as it
 * does the 200 body rather than dropping it back to the stale verdict.
 */
export function setupMarkerErrorBody(
  error: unknown,
): { code?: string; config_saved?: boolean; backend?: AcpBackendProbe } | null {
  if (!(error instanceof ApiError)) return null
  try {
    const body = JSON.parse(error.body)
    return body?.code === 'setup_marker_write_failed' ? body : null
  } catch {
    return null
  }
}

/** The server's marker-write message for a `setup_marker_write_failed`, or `null`. */
export function setupMarkerErrorMessage(error: unknown): string | null {
  return setupMarkerErrorBody(error) ? (error as ApiError).message : null
}

/**
 * Whether a failed config PATCH nonetheless committed the agent choice. The
 * gateway answers 503 `setup_marker_write_failed` with `config_saved: true`
 * when `agent.acp_backend` is on disk and only the first-run marker is not, so
 * the config the caller reads has moved even though the request failed.
 */
export function agentChoiceSaved(error: unknown): boolean {
  return setupMarkerErrorBody(error)?.config_saved === true
}

/**
 * What ONE `GET /api/acp-backends` row says about starting its harness, on its
 * own — no host, sandbox or setup facts.
 *
 * First-run setup and Settings > Agent Harness both decide from this, so the two
 * cannot drift on what a probe row means (#14517). Each keeps the checks only it
 * owns: the setup gate adds the host sandbox, `independent_setup` and KAS's
 * prerequisites; Settings adds the config schema's selectable set.
 *
 * - `unprobed`: no row (query in flight, 403/404, or the payload omits it).
 * - `unselectable`: this build or policy refuses it; a switch would be rejected.
 * - `missing`: its components are not installed on this machine.
 * - `restart_required`: installed, but this gateway cached its absence, so a
 *   session started now still fails until the cache is dropped.
 * - `unknown`: the install check itself failed. NOT `missing`.
 * - `installed`: confirmed installed and spawnable.
 *
 * The order is the precedence: the first reason that applies wins. The gateway
 * only sets `restart_required` on an installed verdict, and the same rule gates
 * the durable setup marker (`kiro_prerequisite.py`: `installed != INSTALLED or
 * restart_required`).
 */
export type AcpProbeState =
  | 'unprobed'
  | 'unselectable'
  | 'missing'
  | 'restart_required'
  | 'unknown'
  | 'installed'

export function acpProbeState(probe: AcpBackendProbe | undefined): AcpProbeState {
  if (!probe) return 'unprobed'
  if (probe.selectable === false) return 'unselectable'
  if (probe.installed === 'missing') return 'missing'
  if (probe.restart_required) return 'restart_required'
  if (probe.installed === 'unknown') return 'unknown'
  return 'installed'
}

/**
 * The row KNOWS the harness cannot start a session now. Settings disables Use on
 * exactly these. `unknown` and `unprobed` are deliberately not blocks: a failed
 * or absent check is not evidence of absence, and disabling on a guess costs the
 * user a switch that would have worked.
 */
export function acpProbeBlocksUse(probe: AcpBackendProbe | undefined): boolean {
  const state = acpProbeState(probe)
  return state === 'unselectable' || state === 'missing' || state === 'restart_required'
}

/**
 * The row POSITIVELY confirms the harness can start. First-run setup requires
 * this before it lets anyone past, because finishing setup is durable: it needs
 * proof, not the absence of a known block, so `unknown` and `unprobed` fail.
 */
export function acpProbeConfirmsUse(probe: AcpBackendProbe | undefined): boolean {
  return acpProbeState(probe) === 'installed'
}
