/**
 * Query key for `GET /api/decisions/provider`, shared by the Decisions card and its
 * lazily loaded model picker. It lives outside the picker module so the card can
 * read the provider without pulling the picker into its own chunk.
 */
export const DECISIONS_PROVIDER_QUERY_KEY = ['decisionsProvider'] as const

/** Preset id for "no decision model"; the gateway's `local_models.PRESET_NONE`. */
export const PRESET_NONE = 'none'

/**
 * Whether the hosted default, or a SageMaker address, is still configured while the
 * ceiling refuses its host. The gateway reports `active: "none"` for it without
 * rewriting the config, so the provider's own `configured_endpoint` is what tells it
 * from a deliberate "No model".
 */
export function isRefusedHostedDefault(data: {
  active: string
  hosted_permitted?: boolean
  configured_refused?: boolean
  configured_endpoint?: string
}): boolean {
  if (data.active !== PRESET_NONE || typeof data.configured_endpoint !== 'string') return false
  const endpoint = data.configured_endpoint.trim()
  if (endpoint === PRESET_NONE) return false
  return data.hosted_permitted === false || (data.configured_refused === true && endpoint.startsWith('sagemaker://'))
}

/** Whether the refused option is the configured SageMaker endpoint, not the hosted default. */
export function isRefusedSageMaker(data: { configured_refused?: boolean; configured_endpoint?: string }): boolean {
  return data.configured_refused === true && (data.configured_endpoint ?? '').trim().startsWith('sagemaker://')
}

/**
 * Whether the model the config names is one the administrator's policy does not
 * allow. "No model" is never refused, nor is an endpoint the owner wrote by hand
 * (`active: "custom"`, which the list does not govern); a refused hosted default or
 * SageMaker address already reads as "No model" (`isRefusedHostedDefault`), so this is
 * the case of a configured local preset the policy refuses.
 */
export function configuredModelRefused(data: Parameters<typeof providerPermitted>[0] & { active: string }): boolean {
  return data.active !== PRESET_NONE && !providerPermitted(data, data.active)
}

/**
 * Whether the policy refuses every model the picker LISTS: the hosted provider, every
 * local preset, and SageMaker when the gateway offers it. "No model" is then the only
 * choice in the list. It says nothing about an endpoint the owner wrote by hand, which
 * the list does not govern and which may still be sent to.
 */
export function allModelsRefused(
  data: Parameters<typeof providerPermitted>[0] & { presets: { id: string }[]; sagemaker_available?: boolean },
): boolean {
  return (
    !providerPermitted(data, PRESET_HOSTED) &&
    data.presets.every(p => !providerPermitted(data, p.id)) &&
    (typeof data.sagemaker_available !== 'boolean' || !providerPermitted(data, 'sagemaker'))
  )
}

/** Preset id for the hosted provider; the gateway's `capability.PROVIDER_HOSTED`. */
export const PRESET_HOSTED = 'jev'
/** The provider read's `active` for an endpoint the owner wrote by hand. Not an
 *  allow-list id: the list does not govern it, so it is always allowed here. */
export const ACTIVE_CUSTOM = 'custom'

/**
 * Whether the administrator's policy allows decision model `id`.
 *
 * The gateway's per-model answer (`provider_permitted`) when it sends one. An older
 * gateway sends the three side answers instead, which are read as before: hosted
 * by `hosted_permitted`, SageMaker by `sagemaker_permitted`, a local preset by
 * `local_permitted`. An absent answer allows, which is what that gateway did.
 * "No model" is always allowed: it sends and runs nothing.
 */
export function providerPermitted(
  data: {
    provider_permitted?: Record<string, boolean>
    hosted_permitted?: boolean
    local_permitted?: boolean
    sagemaker_permitted?: boolean
  },
  id: string,
): boolean {
  if (id === PRESET_NONE || id === ACTIVE_CUSTOM) return true
  const answer = data.provider_permitted?.[id]
  if (typeof answer === 'boolean') return answer
  if (id === PRESET_HOSTED) return data.hosted_permitted !== false
  if (id === 'sagemaker') return (data.sagemaker_permitted ?? data.hosted_permitted) !== false
  return data.local_permitted !== false
}
