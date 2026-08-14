/**
 * Guard: a script imported from `src/` must not open with a shebang.
 *
 * WHY THIS EXISTS. `scripts/check-i18n-strings.mjs` and `scripts/i18n-translate.mjs`
 * began with `#!/usr/bin/env node`, and four i18n tests import them. On Windows,
 * Vite's CJS interop prepends its generated requires to line 1, which pushes the
 * shebang *after* executable code:
 *
 *   const fileURLToPath = __vite__cjsImport3_node_url["fileURLToPath"];#!/usr/bin/env node
 *
 * Rolldown then fails the parse with `Invalid Character '!'`. The failure mode is
 * the bad kind: the importing file collects ZERO tests and reports "no tests"
 * rather than a red assertion, so `unitLiterals`, `localeFormatting`,
 * `untranslatedRatchet` and `translateDriver` silently stop gating anything.
 * On Linux the transform leaves the shebang on its own line, so CI — which runs
 * Linux only — cannot see it. That is precisely why this needs to be a test and
 * not a convention.
 *
 * The shebangs were decorative. Every one of these files is mode 100644 (not
 * executable) and is invoked as `node scripts/<name>.mjs` from package.json, so
 * nothing was relying on them.
 *
 * `scripts/lib/*.mjs` — the modules this repo already extracted so gates could be
 * shared with tests — carry no shebang. This test pins that pattern for the
 * top-level scripts that tests reach into directly.
 */

import { readdirSync, readFileSync, statSync, existsSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { describe, it, expect } from 'vitest'

const SRC = resolve(__dirname, '..')
const WEBSITE = resolve(SRC, '..')

/** Every `.ts`/`.tsx` under `src/`, which is the only place that imports scripts. */
function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(entry)) out.push(full)
  }
  return out
}

/**
 * Matches `from '../../scripts/x.mjs'` at any depth. Deliberately only `scripts/`:
 * a shebang anywhere else is nobody's business, and a script NOT imported by the
 * bundler is free to keep one.
 */
const IMPORT_RE = /from\s+['"](?:\.\.\/)+scripts\/([^'"]+)['"]/g

describe('scripts imported from src (windows transform safety)', () => {
  const imported = new Set<string>()
  for (const file of walk(SRC)) {
    const source = readFileSync(file, 'utf-8')
    for (const m of source.matchAll(IMPORT_RE)) imported.add(m[1])
  }

  it('finds the script imports it is meant to guard', () => {
    // A refactor that moves every import behind `scripts/lib/` would empty this
    // set and make the assertion below vacuously true. Fail loudly instead.
    expect(imported.size).toBeGreaterThan(0)
  })

  it('never imports a script that opens with a shebang', () => {
    const offenders: string[] = []
    for (const rel of [...imported].sort()) {
      const full = join(WEBSITE, 'scripts', rel)
      if (!existsSync(full)) continue // a bare specifier resolved elsewhere
      const firstLine = readFileSync(full, 'utf-8').split('\n', 1)[0]
      if (firstLine.startsWith('#!')) offenders.push(`scripts/${rel}: ${firstLine.trim()}`)
    }
    expect(
      offenders,
      'A shebang on an imported script breaks collection on Windows — Vite prepends its '
      + 'CJS requires to line 1 and Rolldown fails on the "!". Drop the shebang (these '
      + 'files are not executable and run as `node scripts/<name>.mjs`), or move the '
      + 'imported exports into scripts/lib/ the way qa-checks.mjs did.\n  '
      + offenders.join('\n  '),
    ).toEqual([])
  })
})
