/**
 * Literal i18n keys for every rail group and resource type.
 *
 * These are written out rather than assembled from the catalog id on purpose:
 * `check-i18n-keys` forbids ASSEMBLING a key, because a template like
 * `` `apps.x.types.${id}` `` cannot be statically verified to resolve, and
 * i18next renders a missing key as the dotted path itself — so a typo ships as
 * `apps.agentcoreObservatory.types.gatewayRules` in the sidebar instead of
 * failing. A literal map is checkable by that gate and by `deadKeys` in both
 * directions.
 *
 * A type or group absent from these maps falls back to its raw catalog id,
 * which is ugly but honest — better than a blank rail row.
 */

/** Rail group heading keys, matching `catalog.GROUPS`. */
export const GROUP_LABEL_KEY: Record<string, string> = {
  compute: 'apps.agentcoreObservatory.groups.compute',
  evaluation: 'apps.agentcoreObservatory.groups.evaluation',
  memory: 'apps.agentcoreObservatory.groups.memory',
  gateway: 'apps.agentcoreObservatory.groups.gateway',
  tools: 'apps.agentcoreObservatory.groups.tools',
  identity: 'apps.agentcoreObservatory.groups.identity',
  policy: 'apps.agentcoreObservatory.groups.policy',
  config: 'apps.agentcoreObservatory.groups.config',
  registry: 'apps.agentcoreObservatory.groups.registry',
  payment: 'apps.agentcoreObservatory.groups.payment',
}

/** Resource type labels, for all 27 types — children included, since a child
 * renders as a sub-list heading inside its parent's detail. */
export const TYPE_LABEL_KEY: Record<string, string> = {
  'agent-runtimes': 'apps.agentcoreObservatory.types.agent_runtimes',
  'agent-runtime-versions': 'apps.agentcoreObservatory.types.agent_runtime_versions',
  'agent-runtime-endpoints': 'apps.agentcoreObservatory.types.agent_runtime_endpoints',
  harnesses: 'apps.agentcoreObservatory.types.harnesses',
  evaluators: 'apps.agentcoreObservatory.types.evaluators',
  'online-evaluation-configs': 'apps.agentcoreObservatory.types.online_evaluation_configs',
  memories: 'apps.agentcoreObservatory.types.memories',
  gateways: 'apps.agentcoreObservatory.types.gateways',
  'gateway-targets': 'apps.agentcoreObservatory.types.gateway_targets',
  'gateway-rules': 'apps.agentcoreObservatory.types.gateway_rules',
  browsers: 'apps.agentcoreObservatory.types.browsers',
  'browser-profiles': 'apps.agentcoreObservatory.types.browser_profiles',
  'code-interpreters': 'apps.agentcoreObservatory.types.code_interpreters',
  'workload-identities': 'apps.agentcoreObservatory.types.workload_identities',
  'api-key-credential-providers':
    'apps.agentcoreObservatory.types.api_key_credential_providers',
  'oauth2-credential-providers':
    'apps.agentcoreObservatory.types.oauth2_credential_providers',
  'token-vault': 'apps.agentcoreObservatory.types.token_vault',
  'policy-engines': 'apps.agentcoreObservatory.types.policy_engines',
  policies: 'apps.agentcoreObservatory.types.policies',
  'policy-generations': 'apps.agentcoreObservatory.types.policy_generations',
  'policy-generation-assets': 'apps.agentcoreObservatory.types.policy_generation_assets',
  'configuration-bundles': 'apps.agentcoreObservatory.types.configuration_bundles',
  'configuration-bundle-versions':
    'apps.agentcoreObservatory.types.configuration_bundle_versions',
  registries: 'apps.agentcoreObservatory.types.registries',
  'registry-records': 'apps.agentcoreObservatory.types.registry_records',
  'payment-managers': 'apps.agentcoreObservatory.types.payment_managers',
  'payment-connectors': 'apps.agentcoreObservatory.types.payment_connectors',
  'payment-credential-providers':
    'apps.agentcoreObservatory.types.payment_credential_providers',
}

/**
 * Candidate fields for a row's display name, most specific first.
 *
 * The 27 types share no single naming field — some carry `name`, some
 * `agentRuntimeName`, some only an id — so a row's heading is the first of these
 * that is present. Falling through to an id is deliberate: an id identifies the
 * row, which is what the reader needs, whereas a blank heading does not.
 */
export const NAME_FIELDS: readonly string[] = [
  'name',
  'agentRuntimeName',
  'evaluatorName',
  'bundleName',
  'harnessName',
  'browserName',
  'codeInterpreterName',
  'agentRuntimeId',
  'evaluatorId',
  'onlineEvaluationConfigId',
  'memoryId',
  'gatewayId',
  'targetId',
  'ruleId',
  'browserId',
  'profileId',
  'codeInterpreterId',
  'policyEngineId',
  'policyId',
  'policyGenerationId',
  'bundleId',
  'versionId',
  'registryId',
  'recordId',
  'paymentManagerId',
  'paymentConnectorId',
  'harnessId',
  'id',
]

/**
 * Fields worth showing beside the name, when a row happens to carry them.
 *
 * `agentRuntimeVersion` is here because leaving it out silently dropped the
 * version from every runtime row — the list said `orders_agent READY` where it
 * should say `orders_agent v13`, and "which version is live" is one of the few
 * facts a reader comes to this page for.
 */
export const BADGE_FIELDS: readonly string[] = [
  'agentRuntimeVersion',
  'version',
  'versionId',
  'status',
  'evaluatorType',
  'level',
]

/** Fields rendered with a `v` prefix, since a bare `13` reads as a count. */
export const VERSION_FIELDS: readonly string[] = [
  'agentRuntimeVersion',
  'version',
  'versionId',
]

/**
 * The short facts shown beside a row's name.
 *
 * Shared by the top-level rows and the child sub-lists on purpose. When only the
 * top-level rows computed these, a version sub-list rendered thirteen rows that
 * all read `orders_agent`, because the name is identical across versions and
 * the version — the one field that tells them apart — was the field being
 * dropped.
 */
export function rowBadges(row: Record<string, unknown>): string[] {
  const out: string[] = []
  for (const field of BADGE_FIELDS) {
    const value = row[field]
    if (typeof value !== 'string' || !value) continue
    out.push(VERSION_FIELDS.includes(field) ? `v${value}` : value)
  }
  return out
}

/**
 * A unique React key for a row, and the identity its expansion state is held by.
 *
 * The index is ALWAYS part of the key, because no field in these responses can be
 * trusted to be unique. `agentRuntimeName` repeats across every version of one
 * runtime, and `agentRuntimeVersion` only disambiguates a version list. The ARN
 * looks like the safe answer and is not: `list-agent-runtime-versions` returns the
 * same base ARN for all thirteen versions even though the API reference documents a
 * `:version` suffix on that field, so keying on it collapsed every row onto one
 * identity and expanding one row expanded all of them.
 *
 * Deriving uniqueness from content means re-auditing it for each of 27 resource
 * types against responses whose documented shape does not match what ships; the
 * index cannot be wrong. The content is kept in front of it only so the key stays
 * readable while debugging.
 */
export function rowKey(row: Record<string, unknown>, index: number): string {
  return [rowName(row), ...rowBadges(row), String(index)].join('|')
}

/** Pick a row's display name, or '' when the row carries none of the candidates. */
export function rowName(row: Record<string, unknown>): string {
  for (const field of NAME_FIELDS) {
    const value = row[field]
    if (typeof value === 'string' && value) return value
  }
  return ''
}
