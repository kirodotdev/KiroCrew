/**
 * Formatting must consult the app's language, not the browser's.
 *
 * ## The defect
 *
 * `d.toLocaleDateString()`, `d.toLocaleDateString([])` and
 * `d.toLocaleTimeString(undefined, { … })` all mean the same thing: format in the
 * **host** locale. They ignore `dashboard.language` entirely. `LanguageProvider`
 * sets `<html lang>`, but `<html lang>` has no effect on `Intl`, so a dashboard
 * running in Chinese on an en-US browser rendered `7/30/2026` and `Jul 30` inside
 * Chinese UI. `a.localeCompare(b)` has the same flaw for ordering: the sort order
 * of a list of names silently depended on the browser.
 *
 * The fix is `src/i18n/format.ts`, which reads the active language per call.
 *
 * ## The rule
 *
 * A `toLocale*` / `localeCompare` call is a finding when it does **not** say
 * which locale it means:
 *
 *   - `x.toLocaleString()` / `(([]))` / `(undefined, opts)`  → finding
 *   - `a.localeCompare(b)`                                   → finding
 *   - `x.toLocaleDateString('en-US', opts)`                  → allowed
 *   - `a.localeCompare(b, 'en-US')`                          → allowed
 *
 * Naming a locale IS the opt-out, which is why there is no allowlist file. That
 * is deliberate: a machine-parse site — an ISO timestamp sort, a filesystem path
 * sort, a value fed to `Date.parse` on the other side — has to state its pin in
 * the code where a reviewer sees it, rather than in a registry a reviewer has to
 * go and look up. Byte-order comparison (`a < b ? -1 : 1`) is the other correct
 * answer for those, and is not matched by this gate at all.
 *
 * ## Why a bidirectional ratchet and not zero
 *
 * There were ~100 such calls when this gate landed. The seam and the shared
 * helpers migrated first, because a helper fixes every consumer at once; the
 * long tail migrates in later batches. The baseline is the remaining debt and
 * fails in BOTH directions — going up is a regression, going down means the
 * number is stale and must be committed, which is the only thing that keeps a
 * budget shrinking (this repo's eslint budget went 1066 → 1116 for want of the
 * downward half).
 *
 * ## Known false negatives, stated explicitly
 *
 *  1. **Indirection.** `const f = d.toLocaleDateString; f()` is not matched. No
 *     such site exists; the pattern is unnatural in this codebase.
 *  2. **A pinned locale can still be the WRONG locale.** This gate proves a
 *     locale was chosen, not that it was chosen correctly. `toLocaleString('en-US')`
 *     on a user-facing date passes here and is still a bug — that is what review
 *     and the render-time gate in Phase 5 are for.
 *  3. **`Intl.*` constructed directly** with a hardcoded locale outside
 *     `format.ts` is not matched. `utils/tz.ts` legitimately does this to derive
 *     cron day-of-week numbers, and the pin there is load-bearing.
 *  4. **`toFixed` / `String(n)` / `join(', ')`** are outside this gate's scope.
 *     They are not locale-aware APIs at all, so there is nothing syntactic to
 *     detect; they are tracked by the phase plan, not by a matcher.
 */

import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'

const SRC = join(__dirname, '..')

/**
 * Remaining un-migrated host-locale calls.
 *
 * Lower this — never raise it — as batches land. When it reaches 0, delete the
 * baseline and assert `[]` directly, which is the phase's stated acceptance gate
 * ("zero bare `toLocale*`/`localeCompare` outside `format.ts`").
 */
const BASELINE = 97

/** The seam itself. It is where a locale is legitimately resolved. */
const SEAM = new Set(['i18n/format.ts'])

const HOST_LOCALE_METHODS = new Set([
  'toLocaleString',
  'toLocaleDateString',
  'toLocaleTimeString',
])

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'locales') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full)
  }
  return out
}

/**
 * Does this argument express a locale?
 *
 * `undefined` and `[]` are the two spellings of "no locale" that read as though
 * they were an argument, and both are present in this codebase — they are the
 * whole reason this cannot be a simple arity check.
 */
function namesALocale(arg: ts.Expression | undefined): boolean {
  if (!arg) return false
  if (arg.kind === ts.SyntaxKind.UndefinedKeyword) return false
  if (ts.isIdentifier(arg) && arg.text === 'undefined') return false
  if (ts.isArrayLiteralExpression(arg) && arg.elements.length === 0) return false
  return true
}

function hostLocaleCalls(file: string, source: string): number[] {
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const hits: number[] = []

  const visit = (node: ts.Node) => {
    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
      const method = node.expression.name.text
      // `toLocale*`: the locale is argument 0.
      // `localeCompare`: argument 0 is the other string, so the locale is 1.
      const localeArg = method === 'localeCompare' ? node.arguments[1] : node.arguments[0]
      if (
        (HOST_LOCALE_METHODS.has(method) || method === 'localeCompare')
        && !namesALocale(localeArg)
      ) {
        hits.push(sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1)
      }
    }
    ts.forEachChild(node, visit)
  }

  visit(sf)
  return hits
}

describe('formatting follows the app language', () => {
  const files = walk(SRC).filter(
    (f) => !SEAM.has(relative(SRC, f).split('\\').join('/')),
  )

  it('finds source files to scan', () => {
    // A green run that scanned nothing is the failure mode this guards.
    expect(files.length).toBeGreaterThan(300)
  })

  it('detects a host-locale call, so the matcher is known to work', () => {
    // A gate nobody has watched go red is not known to work. These four shapes
    // are exactly the ones found in the codebase.
    const sample = [
      'const a = d.toLocaleDateString()',
      'const b = d.toLocaleTimeString([], { hour: "2-digit" })',
      'const c = n.toLocaleString(undefined, { maximumFractionDigits: 1 })',
      'const e = x.name.localeCompare(y.name)',
    ].join('\n')
    expect(hostLocaleCalls('sample.ts', sample)).toEqual([1, 2, 3, 4])
  })

  it('accepts a call that names its locale', () => {
    const pinned = [
      'const a = d.toLocaleDateString("en-US", { weekday: "short" })',
      'const b = a.localeCompare(b, "en-US")',
      'const c = d.toLocaleString(activeLocale())',
    ].join('\n')
    expect(hostLocaleCalls('pinned.ts', pinned)).toEqual([])
  })

  it(`has exactly ${BASELINE} un-migrated host-locale call(s)`, () => {
    const offenders: string[] = []
    for (const file of files) {
      const rel = relative(SRC, file).split('\\').join('/')
      const source = readFileSync(file, 'utf-8')
      if (!source.includes('toLocale') && !source.includes('localeCompare')) continue
      const lines = source.split('\n')
      for (const lineNo of hostLocaleCalls(rel, source)) {
        offenders.push(`${rel}:${lineNo}  ${(lines[lineNo - 1] ?? '').trim()}`)
      }
    }

    expect(
      offenders.length,
      offenders.length > BASELINE
        ? 'A formatting call here reads the BROWSER\'s locale, not the app\'s language, so it '
          + 'renders English dates inside a translated UI. Use the helpers in `src/i18n/format.ts` '
          + '(`fmtDate`, `fmtTime`, `fmtNumber`, `fmtRelative`, `compareText`). If the value is '
          + 'machine-parsed — an ISO timestamp sort, a filesystem path, anything with a parser on '
          + 'the other side — name the locale explicitly (`toLocaleDateString(\'en-US\', …)`) or '
          + 'compare bytes (`a < b ? -1 : 1`) and say why in a comment.\n\n'
          + offenders.slice(0, 12).join('\n')
        : `Improved — lock it in: set BASELINE = ${offenders.length} in this file.`,
    ).toBe(BASELINE)
  })
})
