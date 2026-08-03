/**
 * A number must not be glued to a unit literal.
 *
 * ## The defect
 *
 * `` `${mins}m ${secs}s` ``, `bytes + ' KB'`, `` `${pct}%` `` — a numeric value
 * concatenated with a hardcoded unit. Three things are wrong with it at once,
 * and none of them is visible in English:
 *
 *  1. **The unit is untranslatable.** `m` is `Min.` in German, `分钟` in Chinese,
 *     `мин` in Russian. A literal in the source is a literal in every language.
 *  2. **The number is not localized.** `hi` groups as `12,34,567`, `bn` renders
 *     Bengali digits, and de/fr/ru want `,` as the decimal separator.
 *  3. **The separator is not localized.** Narrow unit lists are space-joined in
 *     en/ru/fr, comma-joined in de (`6 Min., 38 Sek.`) and joined with NOTHING
 *     in zh — so even a correct pair of parts is assembled wrongly.
 *
 * `src/i18n/format.ts` answers all three: `fmtUnit` for one value, `fmtDuration`
 * for a compound, `fmtBytes` for a size, `fmtPercent` for a ratio.
 *
 * ## Why this gate exists at all
 *
 * Because the existing gates provably do not catch it. `hooks/useUptime.ts`
 * rendered `6m 38s` in all ten languages and escaped BOTH:
 *
 *  - the `no-literal-string` eslint gate, whose Tailwind-shaped `words.exclude`
 *    regex treats `Xm Xs` as a class-name token (a known false-negative class
 *    documented in `eslint.i18n.config.js`), and
 *  - the `en-XA` render detector, which skipped it because the label was solo in
 *    its block and its "unaccented word" signal requires three ASCII letters.
 *
 * Two independent gates, one string, both blind. The pattern is *syntactic*, so
 * an AST check is the right shape — and unlike the two above, this one has no
 * regex over prose to be fooled by.
 *
 * ## The rule
 *
 * Three shapes are findings, all of them "a value with a unit welded on":
 *
 *   - a template literal span:      `` `${mins}m ${secs}s` ``
 *   - string concatenation:         `bytes + ' KB'`
 *   - **JSX text after an expression**: `<span>{pct.toFixed(0)}%</span>`
 *
 * The third is the most idiomatic React spelling and was a false negative in the
 * first cut of this gate — it was caught only because a reviewer flagged a site
 * by hand that the gate had passed. Units are the measurement units this UI
 * actually renders: time (`ms s m h d`), data (`B KB MB GB TB`), rate/ratio
 * (`%`, `x`) and compact suffixes (`K`, `M`).
 *
 * ## What is NOT a finding, and why
 *
 * **CSS values.** `` style={{ width: `${pct}%` }} ``, `el.style.height = h + 'px'`,
 * `` `height ${MS}ms ${EASE}` `` — a localized digit in a CSS length is not a
 * cosmetic problem, it is a **silently dropped value**: `lib/cssSanitize.ts`
 * guards five iframe theme surfaces with `/^[a-zA-Z0-9#(),.\- %/]+$/`, which a
 * Bengali digit fails, so the declaration is discarded entirely. Those sites MUST
 * keep bare arithmetic, so this gate excludes them by context rather than
 * inviting someone to "fix" them.
 *
 * CSS context is recognised as: a `style` JSX attribute, an object property whose
 * key is a CSS property, an assignment into `.style.*` or `setProperty`, a
 * `useMotionTemplate` call, a JSX attribute named like a dimension (`w`, `h`,
 * `size`, `delay` — a component prop that flows into a style downstream), or any
 * literal containing a CSS-only unit (`px em rem vh vw fr deg ch pt`) — a
 * template mixing `px` with `ms` is a transition shorthand, not prose.
 *
 * The dimension-prop exclusion is a NAME heuristic and therefore imprecise in
 * both directions: it cannot see that `<ShimmerLine w={`${x}%`} />` ends up in a
 * `width`, and it would wrongly excuse a genuine label passed as `size`. Deciding
 * properly needs cross-file flow analysis, which a syntactic gate does not do.
 *
 * **Machine formats with a parser on the other side.** `utils/cronUtils.tsx`
 * emits `1h`/`30m` as the wire shape `parseEveryFromSchedule` reads back with
 * `/^every\s+(\d+)\s*([sh])/`, and `utils/pasteTokens.ts` emits
 * `[ Paste #N · M lines ]` parsed back by a `\d+` regex. Localizing either
 * silently breaks the round trip. These are listed in `ROUND_TRIP` below with
 * their parser, because "it looks like display text" is exactly the trap.
 *
 * ## Known false negatives, stated explicitly
 *
 *  1. **Indirection.** A unit held in a variable (`const u = 'm'; `${n}${u}``) is
 *     not matched. No such site exists today.
 *  2. **Words, not symbols.** `` `${n} minutes` `` is not matched — the unit
 *     vocabulary is deliberately symbols-only, because matching English words
 *     would collide with ordinary prose. Those sites are caught instead by the
 *     `no-literal-string` gate, which sees a translatable word.
 *  3. **A number formatted correctly but assembled wrongly.** `${fmtNumber(n)}m`
 *     IS matched (the trailing `m` is still a literal), but
 *     `` `${fmtUnit(a,'hour')} ${fmtUnit(b,'minute')}` `` is not — a hand-joined
 *     pair of correct parts passes. `fmtDuration` exists so callers do not
 *     hand-join; this gate cannot tell that they did.
 *  4. **Scope.** Only `.ts`/`.tsx` under `src`, excluding tests.
 *
 * ## How this is enforced: two diff gates that store nothing, one ceiling
 *
 * `[added-lines]` and `[vs-base]` are the real gate. Both are ZERO-TOLERANCE and
 * store NO count anywhere: they read the base ref, so git supplies the previous
 * state and there is no number in this file for a parallel branch to regenerate.
 *
 *   - `[added-lines]` — a finding on a line this branch wrote. No exemption.
 *   - `[vs-base]` — a file this branch touched holds MORE findings than it did at
 *     the base. Catches the two things line attribution cannot: an edit that turns
 *     an exempt site into a real one without touching the finding's own line, and
 *     an offsetting add-and-remove that leaves the whole-repo count flat.
 *
 * `BASELINE` is the third and weakest — one UPWARD-ONLY whole-repo ceiling over the
 * frozen debt on lines nobody touched, which exists only because a hard zero would
 * fail on arrival. Lowering it is OPTIONAL and never required to land a change.
 * That is the point: an exact-equality ratchet fails every branch that FIXES a site
 * and orders it to edit one shared number, which is a merge conflict between all of
 * them and buys no enforcement the two diff gates do not already provide. The goal
 * is 0, at which point the constant and its test are deleted.
 *
 * Per the standing project rule: this is a syntactic floor, not a measurement of
 * "all unlocalized units". The render-time gate in Phase 5 is the ground truth.
 */

import { describe, it, expect } from 'vitest'
import { execFileSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import ts from 'typescript'

// The hunk parser and its "every line is new" sentinel are imported rather than
// reimplemented, so this gate and `check-i18n-strings.mjs` cannot come to disagree
// about what "a line this branch wrote" means.
import { ALL_LINES, parseAddedLines } from '../../scripts/check-i18n-strings.mjs'

const SRC = join(__dirname, '..')
const REPO = join(SRC, '..', '..')

/**
 * Frozen whole-repo debt: un-migrated number+unit literals on lines nobody has
 * touched since this gate landed.
 *
 * UPWARD-ONLY. Going over fails; coming under does NOT, and lowering this number is
 * OPTIONAL — never required to land a change. That is deliberate: a shared count
 * that every branch must edit as it improves is a merge conflict between all of
 * them, and the `[added-lines]` and `[vs-base]` gates below are what actually stop
 * a new site, measured against the base ref with nothing stored. Never raise it.
 * The goal is 0, at which point this constant and its test are deleted.
 */
const BASELINE = 74

/** The seam is where a unit is legitimately turned into text. */
const SEAM = new Set(['i18n/format.ts'])

/**
 * Machine formats whose output is parsed back. Localizing these breaks the round
 * trip, so they are exempt BY PATH with the parser named. Any addition here owes
 * the same citation.
 */
const ROUND_TRIP = new Map<string, string>([
  ['utils/cronUtils.tsx', 'wire shape read back by parseEveryFromSchedule in components/WeekGrid.tsx'],
  ['utils/pasteTokens.ts', 'token read back by PASTE_TOKEN_REGEX in the same file'],
  ['utils/tz.ts', 'UTC±HH:MM offset token, and en-US pins that derive cron day-of-week numbers'],
])

/** Measurement units this UI renders. Symbols only — see false negative 2. */
const UNIT = /^(ms|s|m|h|d|B|KB|MB|GB|TB|kB|K|M|%|x)(?![a-zA-Z])/

/** Units that only ever mean CSS. A literal containing one is a CSS value. */
const CSS_UNIT = /\d\s*(px|r?em|vh|vw|vmin|vmax|fr|deg|ch|pt|cm|mm|dvh|svh)\b|\bcalc\(/

/** JSX attributes whose value is a CSS length, directly or one hop downstream. */
const CSS_JSX_ATTR = new Set(['style', 'w', 'h', 'width', 'height', 'size', 'delay', 'offset'])

/** Object keys that are CSS properties. */
const CSS_PROPERTY = new Set([
  'width', 'height', 'top', 'left', 'right', 'bottom', 'inset', 'transform', 'transition',
  'animation', 'animationDelay', 'animationDuration', 'transitionDuration', 'transitionDelay',
  'maxWidth', 'minWidth', 'maxHeight', 'minHeight', 'gridTemplateColumns', 'gridTemplateRows',
  'aspectRatio', 'boxShadow', 'padding', 'margin', 'lineHeight', 'fontSize', 'gap', 'flexBasis',
  'strokeDasharray', 'strokeWidth', 'fontWeight', 'borderRadius', 'font', 'filter', 'clipPath',
  'backgroundSize', 'backgroundPosition', 'translate', 'scale', 'rotate', 'offsetDistance',
])

/**
 * The scanned population, as a predicate on a path relative to `src`.
 *
 * `walk()` applies the structural half of this while traversing. The diff-scoped
 * gates get their paths from git instead, so they need the whole predicate in one
 * place — and it has to be the SAME one. A path the ceiling counts but the diff
 * gates skip would be a silent exemption that nothing reports.
 */
function inScope(rel: string): boolean {
  if (!/\.tsx?$/.test(rel) || /\.test\.tsx?$/.test(rel)) return false
  const parts = rel.split('/')
  if (parts.includes('node_modules') || parts[0] === 'locales') return false
  return !SEAM.has(rel) && !ROUND_TRIP.has(rel)
}

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    if (entry === 'node_modules' || entry === 'locales') continue
    const full = join(dir, entry)
    if (statSync(full).isDirectory()) walk(full, out)
    else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) out.push(full)
  }
  return out
}

/** Is this node lexically inside a position whose value is CSS? */
function inCssContext(node: ts.Node): boolean {
  for (let n: ts.Node | undefined = node; n; n = n.parent) {
    // style={{ ... }} or style="..."
    if (ts.isJsxAttribute(n) && CSS_JSX_ATTR.has(n.name.getText())) return true
    // { width: `...` }
    if (ts.isPropertyAssignment(n)) {
      const key = ts.isIdentifier(n.name) || ts.isStringLiteral(n.name) ? n.name.text : ''
      if (CSS_PROPERTY.has(key)) return true
    }
    // el.style.height = ... / setProperty('--x', ...)
    if (ts.isBinaryExpression(n) && n.operatorToken.kind === ts.SyntaxKind.EqualsToken) {
      if (/\.style\b/.test(n.left.getText())) return true
    }
    if (ts.isCallExpression(n)) {
      const callee = n.expression.getText()
      if (/setProperty$/.test(callee) || /useMotionTemplate$/.test(callee)) return true
    }
    // Tagged template: useMotionTemplate`...`
    if (ts.isTaggedTemplateExpression(n) && /MotionTemplate/.test(n.tag.getText())) return true
  }
  return false
}

/** Line numbers where a numeric span is glued to a unit literal. */
export function unitLiteralHits(file: string, source: string): number[] {
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const hits = new Set<number>()
  const line = (n: ts.Node) => sf.getLineAndCharacterOfPosition(n.getStart(sf)).line + 1

  const visit = (node: ts.Node) => {
    // `${expr}UNIT` — the literal chunk that FOLLOWS an interpolation.
    if (ts.isTemplateExpression(node)) {
      const whole = node.getText(sf)
      if (!CSS_UNIT.test(whole) && !inCssContext(node)) {
        for (const span of node.templateSpans) {
          const text = span.literal.text
          // Allow one space between value and unit (`5 KB`), as CLDR does.
          if (UNIT.test(text) || UNIT.test(text.replace(/^ /, ''))) hits.add(line(span.literal))
        }
      }
    }
    // <span>{expr}UNIT</span> — a JSX expression followed by JSX text.
    // This is the most idiomatic React spelling of the defect and was missed by
    // the first cut of this gate, which only looked at template literals and
    // string concatenation.
    if (ts.isJsxElement(node) || ts.isJsxFragment(node)) {
      const kids = node.children
      for (let i = 0; i < kids.length - 1; i++) {
        const cur = kids[i]
        const next = kids[i + 1]
        if (!ts.isJsxExpression(cur) || !ts.isJsxText(next)) continue
        if (inCssContext(cur)) continue
        const text = next.text
        if (CSS_UNIT.test(text)) continue
        if (UNIT.test(text) || UNIT.test(text.replace(/^ /, ''))) hits.add(line(cur))
      }
    }
    // expr + 'UNIT'
    if (
      ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.PlusToken
      && ts.isStringLiteral(node.right)
      && !inCssContext(node)
      && !CSS_UNIT.test(node.right.text)
    ) {
      const text = node.right.text
      if (UNIT.test(text) || UNIT.test(text.replace(/^ /, ''))) hits.add(line(node.right))
    }
    ts.forEachChild(node, visit)
  }

  visit(sf)
  return [...hits].sort((a, b) => a - b)
}

/**
 * What this branch changed, measured live against the base ref.
 *
 * Nothing is stored. Git already holds the previous state, so the base count is
 * computed rather than read from a ledger that can go stale or be re-snapshotted
 * past. Returns `null` only when there is genuinely nothing to diff — a bare local
 * run with `I18N_BASE_REF` unset. CI always supplies it, on a PR and on a push to
 * `main` alike.
 *
 * When a base ref IS configured but cannot be resolved this THROWS rather than
 * returning `null`: a gate that cannot run must fail, not pass. `ci.yml` records
 * having watched a sibling gate skip itself green on a failed fetch.
 *
 * The semantics are copied from `diffScope()` in `scripts/check-i18n-strings.mjs`:
 * the merge-base-then-tip fallback (CI checks out at depth 1, so there is no shared
 * history and `merge-base` fails), untracked files counting as wholly new, and the
 * WORKING TREE as the right-hand side — `base...HEAD` compares commits, so it would
 * exempt exactly the moment a developer runs this locally to check what they wrote.
 */
type Scope = {
  /** Lines this branch wrote, keyed by repo-relative path. `null` means every line. */
  written: Map<string, Set<number> | null>
  /** Paths relative to `src`, filtered to the scanned population. */
  touched: string[]
  /** The file's contents at the base, or `null` if it did not exist there. */
  readBase: (rel: string) => string | null
}

const PREFIX = 'website/src/'

function computeScope(): Scope | null {
  const baseRef = process.env.I18N_BASE_REF
  if (!baseRef) return null
  const git = (args: string[]) =>
    execFileSync('git', args, { cwd: REPO, encoding: 'utf-8', maxBuffer: 128 * 1024 * 1024 })

  try {
    git(['rev-parse', '--verify', `${baseRef}^{commit}`])
  } catch {
    throw new Error(
      `cannot resolve ${baseRef}. The diff-scoped unit-literal gates need the base ref: `
      + 'fetch it before running, or unset I18N_BASE_REF to check only the ceiling.',
    )
  }

  let from: string
  try {
    from = git(['merge-base', baseRef, 'HEAD']).trim()
  } catch {
    from = baseRef
  }

  const raw = parseAddedLines(git(['diff', '-U0', '--no-color', from, '--', 'website/src']))
  const written = new Map<string, Set<number> | null>()
  for (const [repoRel, lines] of Object.entries(raw)) {
    written.set(repoRel, lines === ALL_LINES ? null : (lines as Set<number>))
  }
  // Untracked files produce no hunk, so git never reports them above. On CI the tree
  // is clean and this is empty; locally it is the difference between the gate seeing
  // a file you just created and silently passing it.
  for (const repoRel of git(['ls-files', '--others', '--exclude-standard', '--', 'website/src'])
    .split('\n').filter(Boolean)) {
    written.set(repoRel, null)
  }

  const touched = [...written.keys()]
    .filter((p) => p.startsWith(PREFIX))
    .map((p) => p.slice(PREFIX.length))
    .filter(inScope)
    .sort()

  return {
    written,
    touched,
    // Absent at the base means the file is new on this branch, so its base count is 0.
    // `git show` writes "path does not exist in <sha>" to stderr on that entirely
    // expected path, and execFileSync inherits stderr, so silence it here rather than
    // print a `fatal:` line into CI logs for every file a branch adds.
    readBase: (rel) => {
      try {
        return execFileSync('git', ['show', `${from}:${PREFIX}${rel}`], {
          cwd: REPO,
          encoding: 'utf-8',
          maxBuffer: 128 * 1024 * 1024,
          stdio: ['ignore', 'pipe', 'ignore'],
        })
      } catch {
        return null
      }
    },
  }
}

/**
 * Memoised so the two diff gates share one set of git calls. A throw is NOT cached,
 * so an unresolvable base ref fails both gates rather than only the first.
 */
let cachedScope: Scope | null | undefined
function scope(): Scope | null {
  if (cachedScope === undefined) cachedScope = computeScope()
  return cachedScope
}

/** Findings in the working tree, by path relative to `src`. */
function hitsNow(rel: string): number[] {
  const full = join(SRC, rel)
  if (!existsSync(full)) return []
  return unitLiteralHits(rel, readFileSync(full, 'utf-8'))
}

describe('a number is never glued to a unit literal', () => {
  const files = walk(SRC).filter((f) => inScope(relative(SRC, f).split('\\').join('/')))

  it('finds source files to scan', () => {
    expect(files.length).toBeGreaterThan(300)
  })

  it('detects the shapes that actually shipped, so the matcher is known to work', () => {
    // Every line here is a real defect this repo carried before Phase 4.
    const sample = [
      'const a = `${m}m ${s}s`',              // useUptime
      "const b = bytes + ' KB'",              // formatBytes
      'const c = `${(n / 1000).toFixed(1)}K`', // fmtK
      'const d = `${ms}ms`',                  // fmtDuration
      "const e = Math.round(pct) + '%'",      // percent label
      'const f = <span>{pct.toFixed(0)}%</span>', // the JSX shape a reviewer found by hand
    ].join('\n')
    expect(unitLiteralHits('sample.tsx', sample).length).toBeGreaterThanOrEqual(6)
  })

  it('does not flag CSS values, which must keep bare digits', () => {
    // A localized digit in a CSS length is dropped outright by
    // lib/cssSanitize.ts's Latin-only allowlist — a silent total value loss.
    const css = [
      'const a = <div style={{ width: `${pct}%` }} />',
      "el.style.height = Math.min(h, maxH) + 'px'",
      'const c = { transition: `height ${MS}ms ${EASE}` }',
      'const d = { gridTemplateColumns: `${railWidth}px minmax(0,1fr) auto` }',
      'const e = useMotionTemplate`calc(${f} * (100% - ${KNOB}px))`',
    ].join('\n')
    expect(unitLiteralHits('css.tsx', css)).toEqual([])
  })

  it('does not flag a value already routed through the seam', () => {
    const ok = [
      "const a = fmtDuration([[m, 'minute'], [s, 'second']])",
      "const b = fmtBytes(bytes)",
      "const c = fmtPercent(ratio)",
    ].join('\n')
    expect(unitLiteralHits('ok.tsx', ok)).toEqual([])
  })

  const ADVICE =
    'A number is concatenated with a hardcoded unit here, so the unit cannot be '
    + 'translated, the digits are not localized, and the separator between parts is '
    + 'wrong outside English. Use the helpers in `src/i18n/format.ts`: `fmtUnit` for one '
    + 'value, `fmtDuration([[h, \'hour\'], [m, \'minute\']])` for a compound, `fmtBytes` '
    + 'for a size, `fmtPercent` for a ratio.\n\n'
    + 'If the value is a CSS length or is parsed back by a regex, it MUST keep bare '
    + 'digits — put it in a style/CSS position the detector recognises, or add the file '
    + 'to ROUND_TRIP in this test naming the parser.'

  it(`holds at most ${BASELINE} un-migrated number+unit literal(s)`, () => {
    const offenders: string[] = []
    for (const file of files) {
      const rel = relative(SRC, file).split('\\').join('/')
      const source = readFileSync(file, 'utf-8')
      const lines = source.split('\n')
      for (const lineNo of unitLiteralHits(rel, source)) {
        offenders.push(`${rel}:${lineNo}  ${(lines[lineNo - 1] ?? '').trim().slice(0, 120)}`)
      }
    }

    // UPWARD-ONLY, deliberately. An exact-equality assertion here would fail every
    // branch that FIXES a site and order it to come back and edit `BASELINE` — one
    // shared number that every parallel branch has to rewrite. The regression is
    // covered by the two diff-scoped gates below, which is what licenses relaxing
    // this one; see the ratchet rule in `website/AGENTS.md`.
    expect(
      offenders.length,
      `${ADVICE}\n\n${offenders.slice(0, 15).join('\n')}`,
    ).toBeLessThanOrEqual(BASELINE)
  })

  /**
   * Zero tolerance, no stored state. A finding on a line this branch wrote fails,
   * whatever the ceiling says — so admitting a new site cannot be bought with a
   * baseline edit that reads like routine ledger churn.
   */
  it('[added-lines] no finding sits on a line this branch wrote', () => {
    const s = scope()
    if (!s) return // bare local run: no base commit to diff against

    const introduced: string[] = []
    for (const rel of s.touched) {
      const written = s.written.get(`${PREFIX}${rel}`)
      if (written === undefined) continue
      const full = join(SRC, rel)
      if (!existsSync(full)) continue
      const lines = readFileSync(full, 'utf-8').split('\n')
      for (const lineNo of hitsNow(rel)) {
        // `null` marks a wholly new file: every line in it was written here.
        if (written !== null && !written.has(lineNo)) continue
        introduced.push(`${rel}:${lineNo}  ${(lines[lineNo - 1] ?? '').trim().slice(0, 120)}`)
      }
    }

    expect(
      introduced,
      `${introduced.length} number+unit literal(s) on lines this branch wrote. There is no\n`
      + `baseline to raise for these — ${BASELINE} covers only the frozen debt on lines\n`
      + `nobody touched.\n\n${ADVICE}\n\n${introduced.slice(0, 15).join('\n')}`,
    ).toEqual([])
  })

  /**
   * What line attribution cannot see, by construction:
   *
   *  - An edit that converts an EXEMPT site into a real one without touching the
   *    finding's own line — dropping the `px` half of `` `${a}% ${b}px` `` moves the
   *    remaining `%` out of CSS context from a line above it.
   *  - An offsetting add-and-remove inside one file, which leaves both the whole-repo
   *    count and the ceiling flat.
   *
   * The base count is computed by running the same detector over the file as it was
   * at the base ref, so there is nothing stored and nothing to keep in sync.
   */
  it('[vs-base] no file this branch touched gained a finding versus the base', () => {
    const s = scope()
    if (!s) return

    const grew: string[] = []
    for (const rel of s.touched) {
      const now = hitsNow(rel).length
      const base = s.readBase(rel)
      const then = base === null ? 0 : unitLiteralHits(rel, base).length
      if (now > then) grew.push(`  ${rel}: ${then} → ${now}`)
    }

    expect(
      grew,
      `${grew.length} file(s) you touched hold MORE number+unit literals than they did at\n`
      + 'the base. Measured live against the base ref, not against a committed count, so\n'
      + `re-snapshotting cannot clear it.\n\n${ADVICE}\n\n${grew.join('\n')}`,
    ).toEqual([])
  })
})
