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
 * ## Why an upward-only ceiling plus two diff-scoped gates
 *
 * There were ~100 such calls when this gate landed. The seam and the shared
 * helpers migrated first, because a helper fixes every consumer at once; the
 * long tail migrates in later batches.
 *
 * The ceiling is upward-only rather than failing in both directions: a
 * downward half does not keep a budget shrinking. A stored count is regenerated
 * by one command, so the downward half only ever costs a ledger hunk that reads
 * like routine churn, while every parallel i18n branch has to edit the same line
 * and conflict. The upward-only model, which is strictly stronger:
 *
 *   - `BASELINE` is a CEILING (`toBeLessThanOrEqual`). Going up fails. Going
 *     down is free and needs no commit, so a branch that migrates twenty call
 *     sites does not also have to touch a number three other branches are
 *     editing.
 *   - `[added-lines]` — a host-locale call on a line THIS BRANCH WROTE fails,
 *     with no exemption and no number to regenerate.
 *   - `[vs-base]` — a file this branch TOUCHED holding more host-locale calls
 *     than it did at the base ref fails, which catches a call added on a line
 *     the diff attributes to context.
 *
 * Both diff-scoped gates store nothing: they read the base ref out of git, which
 * already holds yesterday's number. They are skipped, loudly, only when
 * `I18N_BASE_REF` is unset — a bare local run. CI always supplies it: the base
 * branch on a PR, and the commit the push replaced on a push to `main`.
 *
 * When the ceiling reaches 0, delete it and assert `[]` directly, which is the
 * phase's stated acceptance gate ("zero bare `toLocale*`/`localeCompare` outside
 * `format.ts`").
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
 *  5. **Hand-rolled clock and duration strings** — `h.padStart(2,'0') + ':00'`,
 *     `` `${m}m ${s}s` `` — call no locale-aware API, so nothing here can see
 *     them. `unitLiterals.test.ts` is the gate for that shape.
 */

import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'
import { ALL_LINES, parseAddedLines } from '../../scripts/check-i18n-strings.mjs'

const SRC = join(__dirname, '..')
const REPO = join(__dirname, '..', '..', '..')
const PREFIX = 'website/src/'

/**
 * Remaining un-migrated host-locale calls — a CEILING, never an equality.
 *
 * Kept TIGHT against the live count on purpose. Upward-only means an improving
 * branch never has to edit this line, so the only cost of tightening it is paid
 * once, here. It is no longer the last line of defence on a push to `main` — CI
 * supplies a base commit there too, so the two diff-scoped gates below run — but
 * slack in a ceiling is still slack a merge-conflict resolution can spend, and a
 * ceiling well above live is not a ceiling, it is an unread constant.
 *
 * `AGENTS.md` § "A ratchet may only be upward-only if a DIFF-SCOPED gate covers
 * the same defect" is the governing rule, and zero is the goal: at 0 this gets
 * deleted and the assertion becomes `[]`, which is the phase's stated acceptance
 * gate ("zero bare `toLocale*`/`localeCompare` outside `format.ts`").
 */
const BASELINE = 37

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

/** Every host-locale call in the tree, as `rel -> line numbers`. */
function scanTree(files: string[]): Map<string, number[]> {
  const out = new Map<string, number[]>()
  for (const file of files) {
    const rel = relative(SRC, file).split('\\').join('/')
    const source = readFileSync(file, 'utf-8')
    if (!source.includes('toLocale') && !source.includes('localeCompare')) continue
    const hits = hostLocaleCalls(rel, source)
    if (hits.length) out.set(rel, hits)
  }
  return out
}

/**
 * Files this branch touched, and which of their lines it wrote.
 *
 * `null` means there is genuinely nothing to diff against — a bare local run with
 * `I18N_BASE_REF` unset. A ref that IS configured but cannot be
 * resolved throws instead, because a gate that cannot run must fail, not pass.
 */
function diffScope(): { added: Record<string, Set<number> | typeof ALL_LINES>; baseOf: (rel: string) => string | null } | null {
  const baseRef = process.env.I18N_BASE_REF
  if (!baseRef) return null
  const git = (args: string[]) =>
    execFileSync('git', args, { cwd: REPO, encoding: 'utf-8', maxBuffer: 128 * 1024 * 1024 })

  git(['rev-parse', '--verify', `${baseRef}^{commit}`])

  // Prefer the merge base so commits that landed on the base branch after this one
  // forked are not attributed here. CI checks out at depth 1 and fetches the base at
  // depth 1 too, so there is no shared history and `merge-base` fails — fall back to
  // the base tip, which needs only the two trees.
  let from: string
  try {
    from = git(['merge-base', baseRef, 'HEAD']).trim()
  } catch {
    from = baseRef
  }

  // The WORKING TREE is the right-hand side, not `HEAD`: `base...HEAD` compares
  // commits, which would exempt exactly the moment a developer runs this locally to
  // check what they just wrote.
  const added: Record<string, Set<number> | typeof ALL_LINES> =
    parseAddedLines(git(['diff', '-U0', '--no-color', from, '--', 'website/src']))
  for (const rel of git(['ls-files', '--others', '--exclude-standard', '--', 'website/src'])
    .split('\n').filter(Boolean)) {
    added[rel] = ALL_LINES
  }

  const baseOf = (rel: string): string | null => {
    try {
      // A file this branch CREATED has no base blob; that is not an error, and
      // execFileSync inherits stderr, so silence git's own message.
      return execFileSync('git', ['show', `${from}:${PREFIX}${rel}`], {
        cwd: REPO, encoding: 'utf-8', maxBuffer: 128 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'],
      })
    } catch {
      return null
    }
  }
  return { added, baseOf }
}

const FIX =
  'A formatting call here reads the BROWSER\'s locale, not the app\'s language, so it '
  + 'renders English dates inside a translated UI. Use the helpers in `src/i18n/format.ts`: '
  + '`fmtDate` / `fmtTime` / `fmtDateTime` for new copy, or — to port a BARE '
  + '`toLocale*String()` without changing what English renders — `fmtDateNumeric`, '
  + '`fmtTimeNumeric`, `fmtDateTimeNumeric`, which reproduce those platform defaults. '
  + '`fmtDateFields` takes an explicit component set, `fmtNumber`/`fmtRelative`/`compareText` '
  + 'cover the rest. If the value is machine-parsed — an ISO timestamp sort, a filesystem '
  + 'path, a cron field, anything with a parser on the other side — name the locale '
  + 'explicitly (`toLocaleDateString(\'en-US\', …)`) or compare bytes (`a < b ? -1 : 1`) and '
  + 'say why in a comment.'

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

  it(`holds at most ${BASELINE} un-migrated host-locale call(s)`, () => {
    const offenders: string[] = []
    for (const [rel, lines] of scanTree(files)) {
      const source = readFileSync(join(SRC, rel), 'utf-8').split('\n')
      for (const lineNo of lines) {
        offenders.push(`${rel}:${lineNo}  ${(source[lineNo - 1] ?? '').trim()}`)
      }
    }

    // "A decrease is reported and tolerated" is the relaxed-ratchet contract in
    // `AGENTS.md`. Tolerated is the assertion; REPORTED has to be this line, or a
    // batch that migrates twenty call sites leaves no trace in CI output and the
    // ceiling silently drifts away from the live count until it measures nothing.
    if (offenders.length < BASELINE) {
      console.log(
        `[host-locale] ${offenders.length} call(s) remain, under the ceiling of ${BASELINE}. `
        + `Lower BASELINE to ${offenders.length} in src/i18n/localeFormatting.test.ts to keep it `
        + 'measuring something.',
      )
    }

    expect(
      offenders.length,
      `${FIX}\n\n${offenders.slice(0, 12).join('\n')}`,
    ).toBeLessThanOrEqual(BASELINE)
  })

  it('[vs-base] counts, rather than positions, so a shifted line is still caught', () => {
    // What `[vs-base]` adds over `[added-lines]`: a call can land on a line git
    // attributes to context (deletions above it shift the numbering), and then no
    // "added" line holds it. Comparing the COUNT in the file against the base blob
    // sees it anyway. Both gates were watched go red on an injected call before
    // this shipped; this locks the comparison the git plumbing feeds.
    const base = ['const a = 1', 'const b = d.toLocaleDateString()'].join('\n')
    const head = ['const a = n.toLocaleString()', 'const b = d.toLocaleDateString()'].join('\n')
    expect(hostLocaleCalls('x.ts', base)).toHaveLength(1)
    expect(hostLocaleCalls('x.ts', head)).toHaveLength(2)
  })

  it('[added-lines] no host-locale call sits on a line this branch wrote', () => {
    const scope = diffScope()
    if (scope === null) {
      // The sibling `.mjs` gates print this. A test that returns silently is a gate
      // nobody can tell ran — reached on a bare local run, never in CI.
      console.log('[added-lines] skipped — I18N_BASE_REF is unset, so there is no branch to diff.')
      return
    }
    const offenders: string[] = []
    for (const [rel, lines] of scanTree(files)) {
      const added = scope.added[`${PREFIX}${rel}`]
      if (added === undefined) continue
      const source = readFileSync(join(SRC, rel), 'utf-8').split('\n')
      for (const lineNo of lines) {
        if (added !== ALL_LINES && !added.has(lineNo)) continue
        offenders.push(`${rel}:${lineNo}  ${(source[lineNo - 1] ?? '').trim()}`)
      }
    }
    expect(offenders, `${FIX}\n\nThere is no ceiling to raise for these — the line is yours.`).toEqual([])
  })

  it('[vs-base] no file this branch touched gained a host-locale call', () => {
    const scope = diffScope()
    if (scope === null) {
      // The sibling `.mjs` gates print this. A test that returns silently is a gate
      // nobody can tell ran — reached on a bare local run, never in CI.
      console.log('[vs-base] skipped — I18N_BASE_REF is unset, so there is no branch to diff.')
      return
    }
    const head = scanTree(files)
    const grew: string[] = []
    for (const repoRel of Object.keys(scope.added)) {
      if (!repoRel.startsWith(PREFIX)) continue
      const rel = repoRel.slice(PREFIX.length)
      if (!/\.tsx?$/.test(rel) || /\.test\.tsx?$/.test(rel) || SEAM.has(rel)) continue
      const now = head.get(rel)?.length ?? 0
      const base = scope.baseOf(rel)
      const was = base === null ? 0 : hostLocaleCalls(rel, base).length
      if (now > was) grew.push(`${rel}: ${was} → ${now}`)
    }
    expect(grew, `${FIX}\n\nThese files hold MORE host-locale calls than at the base ref.`).toEqual([])
  })
})
