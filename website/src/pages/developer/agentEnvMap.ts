/**
 * The harness env maps Settings > Agent Harness writes (`agent.strands_env`,
 * `agent.deepseek_env`): each value is a `secret://` reference to a vault entry,
 * never the value itself, and the gateway merges each write into the saved map by
 * key.
 */

/** The reference prefix these maps admit, and nothing else. */
export const SECRET_REF_PREFIX = 'secret://'

/** The `agent.<name>` env map as a plain string map; anything else reads as empty. */
export function readAgentEnvMap(cfg: unknown, name: 'strands_env' | 'deepseek_env'): Record<string, string> {
  const agent = (cfg as { agent?: Record<string, unknown> } | undefined)?.agent
  const raw = agent?.[name]
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {}
  const out: Record<string, string> = {}
  for (const [k, v] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof v === 'string') out[k] = v
  }
  return out
}

/** The vault name a mapped value references, or null when it is not a reference. */
export function referencedVaultName(value: string | undefined): string | null {
  if (!value || !value.startsWith(SECRET_REF_PREFIX)) return null
  const name = value.slice(SECRET_REF_PREFIX.length)
  return name ? name : null
}
