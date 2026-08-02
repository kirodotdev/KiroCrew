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

/** Dropdown rows (canonical key + display + window), default first. */
export function displayModels(): { name: string; description: string; contextWindow: number }[] {
  return Object.entries(REGISTRY)
    .filter(([, e]) => PROVIDER in e.providers)
    .sort(([, a], [, b]) => (b.default ? 1 : 0) - (a.default ? 1 : 0))
    .map(([key, e]) => ({ name: key, description: e.display, contextWindow: e.window }))
}
