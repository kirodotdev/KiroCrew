/**
 * `public/vendor/lucide-react.mjs` must name EVERY export of the installed
 * lucide-react.
 *
 * An installed app's UI bundle resolves `import { Pause } from 'lucide-react'`
 * through index.html's import map to that stub, and ESM resolves named imports
 * at LINK time: an icon the stub does not name makes the whole app module fail
 * to load with "does not provide an export named 'Pause'". Nothing in the stub
 * has run at that point, so its Proxy default export cannot stand in, and the
 * app's panel renders nothing until someone notices the console. These
 * assertions are what make a lucide-react bump with no regeneration a red test
 * instead of a broken panel.
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { STUB_PATH, buildStubSource, exportNames } from '../../scripts/gen-vendor-lucide.mjs'

function stubSource(): string {
  return readFileSync(STUB_PATH, 'utf8')
}

function stubNamedExports(): Set<string> {
  const block = stubSource().split('export const {')[1]?.split('} = m')[0]
  if (!block) throw new Error(`${STUB_PATH} has no \`export const { ... } = m\` block`)
  return new Set(block.split(',').map((name) => name.trim()).filter(Boolean))
}

describe('vendor/lucide-react.mjs', () => {
  it('names every export of the installed lucide-react', () => {
    const named = stubNamedExports()
    const missing = exportNames().filter((name: string) => !named.has(name))
    expect(
      missing,
      `${missing.length} lucide-react export(s) are absent from the stub, so an app `
        + 'importing any of them fails to load. Run: npm run gen:vendor-lucide',
    ).toEqual([])
  })

  it('matches the generator byte for byte', () => {
    expect(
      stubSource(),
      'The stub is stale. Run: npm run gen:vendor-lucide',
    ).toBe(buildStubSource())
  })

  it('keeps the Proxy default export, so an unnamed property still resolves', () => {
    expect(stubSource()).toMatch(/export default new Proxy\(m,/)
  })
})
