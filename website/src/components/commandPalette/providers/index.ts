import type { ResourceProvider } from '../types'

/**
 * Command palette provider registry (Search Everywhere).
 *
 * Provider implementations register themselves here (via side-effect import or
 * an explicit {@link registerProvider} call). The `All` aggregator and the
 * CommandPalette tab strip iterate the registry through {@link getProviders}.
 *
 * Mirrors the conventions of `src/surfaces/registry.ts`
 * (`getBuiltinSurfaces` / `_resetBuiltinsForTest`).
 */
const registry: ResourceProvider[] = []

/**
 * Register a provider. Re-registering an id replaces the existing entry
 * (idempotent across hot-reload / repeated side-effect imports).
 */
export function registerProvider(provider: ResourceProvider): void {
  const existing = registry.findIndex((p) => p.id === provider.id)
  if (existing >= 0) {
    registry[existing] = provider
  } else {
    registry.push(provider)
  }
}

/** All registered providers, in registration order. */
export function getProviders(): readonly ResourceProvider[] {
  return registry
}

/** Look up a single provider by id (e.g. for a per-category tab). */
export function getProvider(id: string): ResourceProvider | undefined {
  return registry.find((p) => p.id === id)
}

/** Test-only: clear the registry so each test starts from a clean slate. */
export function _resetProvidersForTest(): void {
  registry.length = 0
}
