/**
 * No two files in one directory may differ in case alone.
 *
 * On a case-insensitive filesystem — macOS by default, and Windows — a pair like
 * `DecisionStrip.tsx` and `decisionStrip.ts` resolves to ONE module. TypeScript
 * then refuses every import of both (`TS1149`, `TS1261`, `TS1192`) and the build
 * stops. This PR shipped exactly that pair and learned it from a red
 * `Build Desktop (macos-15)`.
 *
 * It needs a gate rather than a habit because of WHERE it is invisible: on Linux
 * the two files genuinely differ, so `tsc`, vitest, eslint and the whole frontend
 * lane stay green, and the first failure is the slowest job in CI — one a
 * contributor developing on Linux has no local equivalent for.
 *
 * Two scopes. File NAMES are compared whole: two entries differing only in case
 * cannot both be checked out. Module STEMS are compared without the extension,
 * and only when the stems themselves differ in case, because a specifier carries
 * no extension. `Foo.ts` beside `Foo.tsx` — one stem, same case — is not flagged:
 * that resolves identically everywhere. Nor is a test, spec or `.d.ts` file,
 * which a runner collects by globbing paths and no specifier ever names; two of
 * those already pair this way on the default branch with the macOS leg green.
 *
 * Directory-scoped, because that is the scope of the collapse.
 */
import { readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

import { describe, it, expect } from 'vitest'

const SRC = join(__dirname, '..')
const SKIP = new Set(['node_modules', '__snapshots__'])
const MODULE = /\.tsx?$/
const NOT_A_SPECIFIER_TARGET = /(\.test|\.spec|\.d)\.tsx?$/

/** Every directory under `root`, including `root` itself. */
function directories(root: string): string[] {
  const out = [root]
  for (const entry of readdirSync(root)) {
    if (SKIP.has(entry)) continue
    const full = join(root, entry)
    if (statSync(full).isDirectory()) out.push(...directories(full))
  }
  return out
}

/** Groups of two or more entries sharing one key. */
function collisions(entries: readonly string[], key: (e: string) => string | null): string[][] {
  const grouped = new Map<string, string[]>()
  for (const entry of entries) {
    const k = key(entry)
    if (k !== null) grouped.set(k, [...(grouped.get(k) ?? []), entry])
  }
  return [...grouped.values()].filter(g => g.length > 1).map(g => [...g].sort())
}

/** Names that differ in case alone. */
export function caseOnlyNames(entries: readonly string[]): string[][] {
  return collisions(entries, e => e.toLowerCase())
}

/** Importable modules whose extension-less specifiers collide BECAUSE of case. */
export function caseOnlyStems(entries: readonly string[]): string[][] {
  const stem = (e: string) => e.replace(MODULE, '')
  const importable = (e: string) => MODULE.test(e) && !NOT_A_SPECIFIER_TARGET.test(e)
  return collisions(entries, e => (importable(e) ? stem(e).toLowerCase() : null))
    .filter(group => new Set(group.map(stem)).size > 1)
}

describe('the matcher', () => {
  it('catches both shapes of collapse', () => {
    // The pair that broke this PR's macOS build: the file names differ by more
    // than case, so only the stem rule sees it.
    expect(caseOnlyStems(['DecisionStrip.tsx', 'decisionStrip.ts', 'rowDisclosure.tsx']))
      .toEqual([['DecisionStrip.tsx', 'decisionStrip.ts']])
    expect(caseOnlyNames(['Types.ts', 'types.ts'])).toEqual([['Types.ts', 'types.ts']])
  })

  it('leaves what resolves identically everywhere alone', () => {
    // One stem across two extensions; a test pair no specifier names; non-modules.
    expect(caseOnlyStems(['panel.ts', 'panel.tsx'])).toEqual([])
    expect(caseOnlyStems(['QuickSend.test.tsx', 'quickSend.test.ts'])).toEqual([])
    expect(caseOnlyStems(['Env.d.ts', 'env.d.ts'])).toEqual([])
    expect(caseOnlyStems(['Logo.svg', 'logo.png'])).toEqual([])
    // ...but a real module pair beside the exempt ones is still caught.
    expect(caseOnlyStems(['quickSend.test.ts', 'Wire.ts', 'wire.tsx'])).toEqual([['Wire.ts', 'wire.tsx']])
  })
})

describe('the tree', () => {
  it('has no case-only collision under src', () => {
    expect(directories(SRC).length).toBeGreaterThan(20) // never passes vacuously
    const found: string[] = []
    for (const dir of directories(SRC)) {
      const entries = readdirSync(dir)
      const where = relative(SRC, dir) || '.'
      for (const g of [...caseOnlyNames(entries), ...caseOnlyStems(entries)]) {
        found.push(`  ${where}: ${g.join(' vs ')}`)
      }
    }
    expect(found, `case-only collisions (these break macOS and Windows):\n${found.join('\n')}`).toEqual([])
  })
})
