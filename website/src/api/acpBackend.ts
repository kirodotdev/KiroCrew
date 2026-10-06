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

/** The kiro-cli backend's id (`ACP_BACKEND_KIRO` on the gateway). It is the
 *  empty string, which is also what an UNSET key means: the gateway itself falls
 *  back to kiro-cli when the key is absent, so an absent key names kiro too. */
export const ACP_BACKEND_KIRO = ''

/** The KAS backend's id (`ACP_BACKEND_KAS` on the gateway): kiro-cli's relay,
 *  which completes first-run setup on kiro-cli ACP support, not a kiro-cli login. */
export const ACP_BACKEND_KAS = 'kas'

/** The slice of the config body this check reads. */
export interface AcpBackendConfig {
  agent?: { acp_backend?: string }
}

/**
 * True only when a LOADED config names the kiro-cli backend. `undefined` (the
 * config has not loaded, or failed to) is not kiro: nothing Kiro-only should
 * render on a guess, and the pill's dash is exactly such a thing.
 */
export function isKiroBackend(cfg: AcpBackendConfig | undefined): boolean {
  if (cfg === undefined) return false
  return (cfg.agent?.acp_backend ?? ACP_BACKEND_KIRO) === ACP_BACKEND_KIRO
}

/** Shared translated harness labels; unknown harnesses keep the server's policy name. */
export function acpBackendName(backend: { id: string; policy_id?: string }): string {
  switch (backend.id) {
    case ACP_BACKEND_KIRO: return i18nT('pages.developer.agentBackendTab.kiro_cli')
    case 'claude': return i18nT('pages.developer.agentBackendTab.claude_code')
    case ACP_BACKEND_KAS: return i18nT('pages.developer.agentBackendTab.kas_kiro_agent')
    default: return backend.policy_id || backend.id
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
