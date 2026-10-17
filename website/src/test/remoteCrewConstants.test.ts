/**
 * The two constants in `utils/remoteCrew.ts` are hand-mirrored from the Python
 * backend, and main has already drifted once. This pins each mirror against the
 * Python source it names, so a backend change fails a frontend test instead of
 * silently shipping a wrong default.
 *
 * Dependency-free by design: the Python files are read as text and the
 * declarations extracted with anchored regexes. A refactor that renames or
 * moves a declaration fails the extraction loudly rather than passing vacuously.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { describe, it, expect } from 'vitest'

import { WARM_SET_CAP_AUTO_CEILING, BUILTIN_PROVISIONER_ID } from '../utils/remoteCrew'

const read = (rel: string): string =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const extract = (source: string, pattern: RegExp, what: string): string => {
  const m = source.match(pattern)
  if (!m) throw new Error(`could not find ${what} — did the declaration move or change shape?`)
  return m[1]
}

describe('remoteCrew.ts mirrors of backend constants', () => {
  it('WARM_SET_CAP_AUTO_CEILING equals its Python namesake in instances/constants.py', () => {
    const py = read('../../../src/kiro_crew/instances/constants.py')
    const ceiling = extract(
      py,
      /^WARM_SET_CAP_AUTO_CEILING: int = (\d+)$/m,
      'WARM_SET_CAP_AUTO_CEILING in src/kiro_crew/instances/constants.py',
    )
    expect(WARM_SET_CAP_AUTO_CEILING).toBe(Number(ceiling))
  })

  it('BUILTIN_PROVISIONER_ID equals BUILTIN_PROVISIONER_ID in platform/interfaces.py', () => {
    const py = read('../../../src/kiro_crew/platform/interfaces.py')
    const id = extract(
      py,
      /^BUILTIN_PROVISIONER_ID(?::\s*str)?\s*=\s*"([^"]+)"/m,
      'BUILTIN_PROVISIONER_ID in src/kiro_crew/platform/interfaces.py',
    )
    expect(BUILTIN_PROVISIONER_ID).toBe(id)
  })
})
