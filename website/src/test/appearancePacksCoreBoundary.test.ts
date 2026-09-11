/**
 * Core must not depend on an OPTIONAL APP.
 *
 * A crew can wear an appearance pack, so the dashboard's own avatar needs the
 * pack format's vocabulary, its two players and its detail reader. Crew Companion
 * and Mochi are apps the user can disable or remove, so anything core needs MOVES
 * to core and the app imports it from there — never the other way round.
 *
 * This is the half of that rule a reviewer cannot see in a diff: an import added
 * to one core file years from now reads as a one-line convenience and silently
 * makes a crew's face depend on an app that may not be installed.
 *
 * Scoped to the two named apps rather than to `apps/` as a whole, because
 * `apps/*.ts` at the top level is the app PLATFORM (the registry, the slot map,
 * the icon table) which core legitimately drives.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

const SRC = resolve(__dirname, '..')

/** The directories that make up core, and the apps they may not reach into. */
const CORE_DIRS = ['components', 'lib', 'hooks', 'pages']
const FORBIDDEN = ['apps/crew-companion', 'apps/mochi']

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) {
      out.push(...sourceFiles(full))
      continue
    }
    // Tests are exempt: a core test may legitimately assert something about an
    // app, and a test file is not shipped code.
    if (!/\.(ts|tsx)$/.test(entry) || /\.test\.tsx?$/.test(entry)) continue
    out.push(full)
  }
  return out
}

/** Every module specifier a file imports or re-exports. */
function specifiers(text: string): string[] {
  const out: string[] = []
  const re = /(?:from|import)\s*\(?\s*['"]([^'"]+)['"]/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) out.push(m[1])
  return out
}

describe('core never imports from an optional app', () => {
  it('has no core module reaching into crew-companion or mochi', () => {
    const offenders: string[] = []
    for (const dir of CORE_DIRS) {
      for (const file of sourceFiles(join(SRC, dir))) {
        const rel = file.slice(SRC.length + 1)
        for (const spec of specifiers(readFileSync(file, 'utf-8'))) {
          // Resolve the specifier against the importing file so a `../../apps/...`
          // is compared as the path it actually names.
          const target = spec.startsWith('.')
            ? resolve(file, '..', spec).slice(SRC.length + 1).replace(/\\/g, '/')
            : spec
          if (FORBIDDEN.some((app) => target === app || target.startsWith(`${app}/`))) {
            offenders.push(`${rel} -> ${spec}`)
          }
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it('scans a real set of core files, so a passing run is not a vacuous one', () => {
    // Without this, moving or renaming a core directory would empty the scan
    // above and it would keep reporting success while checking nothing.
    const counted = CORE_DIRS.map((dir) => sourceFiles(join(SRC, dir)).length)
    expect(Math.min(...counted)).toBeGreaterThan(5)
  })

  it('keeps the pack players and the pack reader in core', () => {
    // The move itself: these are what the Companion now imports FROM core, so a
    // revert that puts them back under the app is caught here rather than by a
    // crew's face going blank once the app is disabled.
    for (const file of [
      'components/appearancePacks/LottieRenderer.tsx',
      'components/appearancePacks/SpriteRenderer.tsx',
      'components/appearancePacks/PackAvatar.tsx',
      'lib/appearancePacks/detail.ts',
      'hooks/usePackDetail.ts',
    ]) {
      expect(statSync(join(SRC, file)).isFile()).toBe(true)
    }
  })
})
