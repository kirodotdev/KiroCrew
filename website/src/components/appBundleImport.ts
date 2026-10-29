/**
 * The one dynamic `import()` AppHost makes for an app's ESM bundle.
 *
 * Its own module so a test can stand in a promise it settles on its own clock
 * (`vi.mock('../components/appBundleImport')`) and observe the loading fallback
 * at a chosen moment. A real import of a missing path settles at whatever speed
 * the test runner's module loader happens to have, which no timing assertion
 * can be pinned to.
 */
import type { ComponentType } from 'react'

export function importAppBundle(path: string): Promise<{ default: ComponentType }> {
  return import(/* @vite-ignore */ path)
}
