/**
 * The Strands harness's model providers, as Settings > Agent Harness offers them.
 *
 * The provider ids and every environment-variable name below are the Strands
 * CLI's own (`@strands-agents/cli`, `tui/view/setup-wizard/providers.js` and
 * `tui/provider/environment.js`). The gateway accepts only those names under
 * `agent.strands_env`, so a name spelled here that the CLI does not read is a
 * value the session would refuse at start.
 *
 * Every value is stored in the Secrets vault and only its `secret://` reference
 * is written to `agent.strands_env`: the config refuses a literal value there, a
 * region or a URL included, so the non-credential settings take the same path as
 * the keys and are equally write-only once saved.
 */

/** The config path the card writes the reference map to. */
export const STRANDS_ENV_KEY = 'agent.strands_env'

/** The reference prefix `agent.strands_env` admits, and nothing else. */
export const SECRET_REF_PREFIX = 'secret://'

/** One value the provider reads from its environment. */
export interface StrandsProviderField {
  /** The environment-variable name the Strands CLI reads. */
  env: string
  /** A credential (masked while typed) rather than a plain setting such as a region. */
  secret: boolean
  /** Whether the provider works without it. */
  optional: boolean
}

/** One Strands model provider. `id` is the CLI's own provider id. */
export interface StrandsProvider {
  id: 'bedrock' | 'bedrock-mantle' | 'anthropic' | 'openai' | 'google' | 'ollama' | 'litellm'
  /** Whether the provider signs in through the AWS credential chain when no field is set. */
  awsChain: boolean
  fields: readonly StrandsProviderField[]
}

// A Bedrock API key and its region. No profile field: the agent's sandbox hides
// the host's AWS config and SSO files from the Strands child, so a profile name
// cannot resolve there.
const AWS_FIELDS: readonly StrandsProviderField[] = [
  { env: 'AWS_BEARER_TOKEN_BEDROCK', secret: true, optional: false },
  { env: 'AWS_REGION', secret: false, optional: false },
]

/** In the CLI's own order, which is also the order the picker lists them in. */
export const STRANDS_PROVIDERS: readonly StrandsProvider[] = [
  { id: 'bedrock', awsChain: true, fields: AWS_FIELDS },
  { id: 'bedrock-mantle', awsChain: true, fields: AWS_FIELDS },
  { id: 'anthropic', awsChain: false, fields: [{ env: 'ANTHROPIC_API_KEY', secret: true, optional: false }] },
  { id: 'openai', awsChain: false, fields: [{ env: 'OPENAI_API_KEY', secret: true, optional: false }] },
  { id: 'google', awsChain: false, fields: [{ env: 'GEMINI_API_KEY', secret: true, optional: false }] },
  { id: 'ollama', awsChain: false, fields: [{ env: 'OLLAMA_HOST', secret: false, optional: true }] },
  {
    id: 'litellm',
    awsChain: false,
    fields: [
      { env: 'LITELLM_BASE_URL', secret: false, optional: false },
      { env: 'LITELLM_API_KEY', secret: true, optional: true },
    ],
  },
]

/** The provider a fresh install starts on: the CLI's own default. */
export const DEFAULT_STRANDS_PROVIDER = STRANDS_PROVIDERS[0]

/** The vault entry this card stores *env* under. One fixed name per variable, so
 *  saving again replaces the value rather than leaving the old one behind. */
export function strandsVaultName(env: string): string {
  return `STRANDS_${env}`
}

/** `agent.strands_env` as a plain string map; anything else reads as empty. */
export function readStrandsEnv(cfg: unknown): Record<string, string> {
  const agent = (cfg as { agent?: unknown } | undefined)?.agent
  const raw = (agent as { strands_env?: unknown } | undefined)?.strands_env
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

/** The config path the provider picker writes. */
export const STRANDS_PROVIDER_KEY = 'agent.strands_provider'

/** The config path the model field writes. */
export const STRANDS_MODEL_KEY = 'agent.strands_model'

/** The model value that names no model: the backend passes no `--model`, and the
 *  provider's own default runs. The setting's default. */
export const STRANDS_MODEL_AUTO = 'auto'

/** `agent.strands_provider` as a provider: empty, unset or unknown reads as Bedrock. */
export function readStrandsProvider(cfg: unknown): StrandsProvider {
  const agent = (cfg as { agent?: unknown } | undefined)?.agent
  const raw = (agent as { strands_provider?: unknown } | undefined)?.strands_provider
  const id = typeof raw === 'string' ? raw.trim().toLowerCase() : ''
  return STRANDS_PROVIDERS.find(p => p.id === id) ?? DEFAULT_STRANDS_PROVIDER
}

/** `agent.strands_model` as the field shows it: empty for `auto` or unset. */
export function readStrandsModel(cfg: unknown): string {
  const agent = (cfg as { agent?: unknown } | undefined)?.agent
  const raw = (agent as { strands_model?: unknown } | undefined)?.strands_model
  const model = typeof raw === 'string' ? raw.trim() : ''
  return model === STRANDS_MODEL_AUTO ? '' : model
}

/**
 * Whether new sessions need a model named for *provider*. With `auto` the backend
 * passes no `--model`, and the project then runs its own default, a Bedrock model,
 * whatever provider is picked. So every provider but Bedrock needs a model id.
 */
export function strandsModelRequired(provider: StrandsProvider): boolean {
  return provider.id !== DEFAULT_STRANDS_PROVIDER.id
}
