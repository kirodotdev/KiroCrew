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
