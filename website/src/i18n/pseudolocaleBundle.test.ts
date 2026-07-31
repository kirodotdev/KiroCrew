/**
 * The pseudolocale catalog must not ship in a production bundle.
 *
 * `en-XA` is a mechanical accent-and-pad transform of the whole English catalog
 * — the largest single catalog in the app (~88 KB gzip) — and it is already
 * unreachable in production: `devOnly` keeps it out of the picker and
 * `isRestorableLanguage()` refuses it from persisted state. Registering it
 * unconditionally therefore cost every production user a catalog none of them
 * can select.
 *
 * `import.meta.env.DEV` is true under vitest, so the production branch is
 * STUBBED and the module re-evaluated rather than merely asserted around: an
 * `if (DEV)`-shaped test would only ever exercise its dev half in CI and would
 * pass just as happily against the unfixed code. This mirrors the stubbing in
 * `pseudolocale.test.ts`.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

import { SUPPORTED_LANGUAGES } from './languages'

const PSEUDO = 'en-XA'
const AUTHORED = SUPPORTED_LANGUAGES.filter(l => !l.devOnly).map(l => l.code)

/** Re-evaluate the i18n module with `import.meta.env.DEV` pinned to `dev`. */
async function catalogsWithDev(dev: boolean): Promise<Record<string, unknown>> {
  vi.stubEnv('DEV', dev)
  vi.resetModules()
  const mod = await import('./index')
  return mod.CATALOGS as Record<string, unknown>
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('pseudolocale catalog is dev-only', () => {
  it('is registered in a dev build', async () => {
    const catalogs = await catalogsWithDev(true)
    expect(Object.keys(catalogs)).toContain(PSEUDO)
  })

  it('is absent from a production build', async () => {
    const catalogs = await catalogsWithDev(false)
    expect(Object.keys(catalogs)).not.toContain(PSEUDO)
  })

  it('leaves every authored language registered in a production build', async () => {
    // The complement: the gate must drop the pseudolocale ONLY. A production
    // build missing a real catalog would render bare keys.
    const catalogs = await catalogsWithDev(false)
    for (const code of AUTHORED) expect(Object.keys(catalogs)).toContain(code)
    expect(Object.keys(catalogs)).toHaveLength(AUTHORED.length)
  })

  it('gates the catalog on the build flag, not on a runtime language check', async () => {
    // Registration must be what changes: keeping the entry and filtering it at
    // selection time would leave the JSON in the bundle, which is the entire
    // cost being removed here.
    const dev = await catalogsWithDev(true)
    const prod = await catalogsWithDev(false)
    expect(Object.keys(dev).length - Object.keys(prod).length).toBe(1)
  })
})
