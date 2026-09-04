// SLIM static fallback source for AcpAdapter.fetchAvailableModels
// (gateway-restart / kiro-cli cold-start timeout on /api/models).
// displayModels() is the only consumer-backed export — there is no full
// registry reader (defaultModel / modelLabel / modelSupportsAuto /
// contextWindow and their indexes).
// Imports the SAME data file the Python backend uses (model_registry.json,
// parity-guarded by test_model_registry_parity.py) so canonical keys,
// display names, and context windows agree without an API round-trip.
import registryRaw from '../model_registry.json'

type Entry = {
  display: string
  description?: string
  window: number
  default?: boolean
  supports_effort?: boolean
  supports_auto?: boolean
  aliases?: string[]
  providers: Record<string, string>
}

const REGISTRY: Record<string, Entry> = Object.fromEntries(
  Object.entries(registryRaw as Record<string, unknown>).filter(([k]) => !k.startsWith('_')),
) as Record<string, Entry>

// NOTE: 'claude_code' here is the model_registry.json providers-map KEY — the
// canonical model-id namespace shared with the backend — NOT a selectable
// provider (the fork is KiroACP/kiro-cli only). Keep the literal verbatim.
const PROVIDER = 'claude_code'

// Per-provider "canonical key / alias / provider id -> canonical key" indices,
// mirroring `_CANONICAL_INDEX` in `model_registry.py` (`_build_indices`): for
// each provider, every entry's own key resolves to itself, its provider id
// resolves to the key, and each alias resolves to the key (first alias wins on
// collision, matching Python's `setdefault`). Built for BOTH the acp (kiro-cli)
// and claude_code namespaces so resolution can prefer the acp view.
const CANONICAL_INDEX: Record<string, Record<string, string>> = (() => {
  const indices: Record<string, Record<string, string>> = {}
  for (const [key, entry] of Object.entries(REGISTRY)) {
    for (const [provider, pid] of Object.entries(entry.providers ?? {})) {
      const idx = (indices[provider] ??= {})
      idx[key] = key // canonical key resolves to itself
      if (pid) idx[pid] = key // provider id -> canonical
      for (const alias of entry.aliases ?? []) {
        if (!(alias in idx)) idx[alias] = key // alias -> canonical (first wins)
      }
    }
  }
  return indices
})()

// Resolution order: the acp (kiro-cli) index FIRST, then claude_code — the SAME
// order `model_registry._registry_window` / `canonical_key` use on the backend,
// and for the same reason. The claude_code index DELIBERATELY aliases kiro's
// distinct models onto one canonical for claude-agent-acp dropdown dedup
// (`claude-haiku-4.5` / `claude-sonnet-4.5` / `claude-sonnet-4` -> `sonnet-4.6-1m`;
// `claude-opus-4.6` -> `opus-4.8-1m`), while kiro — the fork's shipping harness —
// serves each as a DISTINCT real model (`haiku-4.5`, `sonnet-4.5`, `sonnet-4`,
// `opus-4.6-1m`). Resolving the acp view first keeps them apart, so the shared
// "same model?" fold cannot equate them (the #5339 harm on the first-class path).
const PROVIDER_ORDER = ['acp', 'claude_code'] as const

// Region/vendor routing prefix a Bedrock inference-profile id carries
// (`global.anthropic.claude-opus-4-8[1m]`, `us.anthropic.…`). A provider-prefixed
// canonical id is NOT itself a registry key/alias, so `canonicalKey` peels the
// prefix and retries the lookup — the "fold a provider/partition prefix" half of
// #5339. Exported as the ONE spelling of this pattern: `fmtTurnModel`
// (chat/AssistantMessage.tsx) trims the same prefix for the footer label, and
// the backend mirrors it in `model_registry._ROUTING_PREFIX_RE`.
export const ROUTING_PREFIX_RE = /^(?:(?:us|eu|apac|global)\.)?(?:anthropic|amazon|openai|bedrock)\./

function lookup(id: string): string | null {
  for (const provider of PROVIDER_ORDER) {
    const key = CANONICAL_INDEX[provider]?.[id]
    if (key !== undefined) return key
  }
  return null
}

/** Resolve a model id (canonical key, registry alias, or per-provider id — with
 *  or without a region/vendor routing prefix) to its canonical registry key, or
 *  `null` when the registry lists nothing matching.
 *
 *  This is the single "same model?" fold shared with the backend
 *  (`model_registry.canonical_key` / `_normalize_model_key`). It resolves the
 *  acp index first so kiro's distinct models stay distinct, keeps the 1M/200K
 *  variants that are DISTINCT canonical entries apart (`claude-opus-4-8` is the
 *  200K `opus-4.8` while `claude-opus-4.8` / `claude-opus-4-8[1m]` are the 1M
 *  `opus-4.8-1m`), and folds a bare alias together with its provider-prefixed
 *  canonical id (`us.anthropic.claude-opus-4-8[1m]` and `claude-opus-4.8` both
 *  -> `opus-4.8-1m`).
 */
export function canonicalKey(name: string): string | null {
  // Registry lookups are exact and its keys/aliases/provider-ids are all
  // lowercase, so resolve on the lowercased id (the string fold lowercases too).
  const raw = (name || '').trim().toLowerCase()
  if (!raw) return null
  const direct = lookup(raw)
  if (direct !== null) return direct
  // Peel a KNOWN routing prefix and retry — a provider-prefixed id names the
  // same model (safe). We deliberately do NOT also peel a revision suffix
  // (`-v1:0`) to force a registry hit: that would INFER a specific context-window
  // entry (200K vs 1M) for an on-demand id whose window the registry does not
  // actually carry, risking a false downgrade amber (GPT review on #6280). A
  // revision-suffixed id therefore stays unregistered and is handled by the
  // conservative heuristic in `isModelDowngrade`.
  const stripped = raw.replace(ROUTING_PREFIX_RE, '')
  return stripped !== raw ? lookup(stripped) : null
}

/** Dropdown rows (canonical key + display + window), default first. */
export function displayModels(): { name: string; description: string; contextWindow: number }[] {
  return Object.entries(REGISTRY)
    .filter(([, e]) => PROVIDER in e.providers)
    .sort(([, a], [, b]) => (b.default ? 1 : 0) - (a.default ? 1 : 0))
    .map(([key, e]) => ({ name: key, description: e.display, contextWindow: e.window }))
}
