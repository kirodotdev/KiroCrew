/**
 * The one dynamic `import()` an app's ESM bundle is loaded through (AppHost,
 * startup modules, SessionControlHost).
 *
 * Its own module so a test can stand in a promise it settles on its own clock
 * (`vi.mock('../components/appBundleImport')`) and observe the loading fallback
 * at a chosen moment. A real import of a missing path settles at whatever speed
 * the test runner's module loader happens to have, which no timing assertion
 * can be pinned to.
 */
import type { ComponentType } from 'react'
import { loadAppRegistry } from './appRegistryPrefetch'

export async function importAppBundle<P = object>(
  path: string,
  // false for a mount that must not reload the page (a startup module).
  { demanded = true }: { demanded?: boolean } = {},
): Promise<{ default: ComponentType<P> }> {
  // An app bundle reads window.__kirocrew_modules as it imports, so the registry
  // goes first. It is its own chunk because it holds every lucide icon (#18561),
  // which the dashboard's first load does not need.
  await loadAppRegistry({ demanded })
  return import(/* @vite-ignore */ path)
}
