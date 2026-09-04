/**
 * Number+unit literal gate.
 *
 * Three checks, strongest first:
 *
 *   [added-lines]  ZERO tolerance, no stored state. A finding on a line this branch
 *                  wrote fails, whatever the ceiling says.
 *   [vs-base]      No file this branch touched may hold MORE findings than it did at
 *                  the base ref. Catches what line attribution cannot: an edit that
 *                  turns an exempt site into a real one without touching the
 *                  finding's own line, and an offsetting add-and-remove.
 *   [ceiling]      One whole-repo count over the frozen debt on lines nobody touched.
 *                  Reports; does not fail. See the note at the bottom.
 *
 * WHY THIS IS A SCRIPT AND NOT A TEST. It parses the whole in-scope tree with the
 * TypeScript compiler, so its cost scales with the size of the repo rather than with
 * what a branch changed. Inside vitest that cost is multiplied by v8 coverage
 * instrumentation and by every sibling worker competing for cores. Measured on one
 * 6-core box, same commit, only --maxWorkers varying:
 *
 *   standalone 2.0s | 2w 11.2s | 4w 13.7s | 6w 16.6s | 8w 20.3s | 12w 28.6s
 *
 * against a 15s per-test budget sized for tests that `await import(...)`. The cost
 * tracks worker count, so raising the budget only moves the number; throttling
 * workers instead costs the whole suite 82% more wall clock. Out here it runs once,
 * in one process, uninstrumented, with no timeout to tune.
 *
 * The matcher itself stays under test: `src/i18n/unitLiterals.test.ts` proves it
 * detects the shapes that shipped and exempts CSS and the seam, importing the same
 * `scripts/lib/unit-literals.mjs` this gate uses. One definition, two consumers.
 *
 * The diff-scoped logic below is carried over UNCHANGED from the test it replaces,
 * including its reliance on `parseAddedLines`. This is a refactor: same findings,
 * same attribution, same limitations. Known limitations it inherits are listed in
 * the accompanying issue rather than silently fixed here.
 */

import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join, relative } from 'node:path'

import { ALL_LINES, parseAddedLines } from './check-i18n-strings.mjs'
import { inScope, unitLiteralHits, walk } from './lib/unit-literals.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const WEBSITE = join(HERE, '..')
const SRC = join(WEBSITE, 'src')
const REPO = join(WEBSITE, '..')
const PREFIX = 'website/src/'

/**
 * Frozen whole-repo debt: un-migrated number+unit literals on lines nobody has
 * touched since this gate landed.
 *
 * UPWARD-ONLY, and lowering it is OPTIONAL, never required to land a change. A
 * shared count that every branch must edit as it improves is a merge conflict
 * between all of them; the two diff-scoped checks are what actually stop a new site.
 * Never raise it. The goal is 0, at which point this constant is deleted.
 */
const BASELINE = 74

const ADVICE =
  'A number is concatenated with a hardcoded unit here, so the unit cannot be '
  + 'translated, the digits are not localized, and the separator between parts is '
  + 'wrong outside English. Use the helpers in `src/i18n/format.ts`: `fmtUnit` for one '
  + "value, `fmtDuration([[h, 'hour'], [m, 'minute']])` for a compound, `fmtBytes` "
  + 'for a size, `fmtPercent` for a ratio.\n\n'
  + 'If the value is a CSS length or is parsed back by a regex, it MUST keep bare '
  + 'digits: put it in a style/CSS position the detector recognises, or add the file '
  + 'to ROUND_TRIP in scripts/lib/unit-literals.mjs naming the parser.'

const git = (args) =>
  execFileSync('git', args, { cwd: REPO, encoding: 'utf-8', maxBuffer: 128 * 1024 * 1024 })

/** Findings in the working tree, by path relative to `src`. */
function hitsNow(rel) {
  const full = join(SRC, rel)
  if (!existsSync(full)) return []
  return unitLiteralHits(rel, readFileSync(full, 'utf-8'))
}

/**
 * What this branch changed, measured live against the base ref. Nothing is stored.
 *
 * Returns null when there is genuinely nothing to diff (I18N_BASE_REF unset, a bare
 * local run). When a base ref IS configured but cannot be resolved this exits 2: a
 * gate that cannot run must fail, not skip itself green on a failed fetch.
 */
function computeScope() {
  const baseRef = process.env.I18N_BASE_REF
  if (!baseRef) return null

  try {
    git(['rev-parse', '--verify', `${baseRef}^{commit}`])
  } catch {
    console.error(
      `[unit-literals] cannot resolve ${baseRef}. The diff-scoped checks need the base `
      + 'ref: fetch it before running, or unset I18N_BASE_REF to check only the ceiling.',
    )
    process.exit(2)
  }

  let from
  try {
    from = git(['merge-base', baseRef, 'HEAD']).trim()
  } catch {
    from = baseRef
  }

  const raw = parseAddedLines(git(['diff', '-U0', '--no-color', from, '--', 'website/src']))
  const written = new Map()
  for (const [repoRel, lines] of Object.entries(raw)) {
    written.set(repoRel, lines === ALL_LINES ? null : lines)
  }
  // Untracked files produce no hunk, so git never reports them above.
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

// ── run ──────────────────────────────────────────────────────────────────────
//
// Output contract: the marker lines below are parsed by `scripts/lib/i18n-gate-table.mjs`.
// The two diff-scoped markers print ONLY when there was a base to diff against, because
// printing "[added-lines] 0" with no base would claim a check that never ran, and it
// would match the table's `find` regex, short-circuiting the table's own
// `!base && scope === 'diff' -> NOT RUN` branch. Staying silent is what lets the
// table say NOT RUN, which is both true and already tested.
const asJson = process.argv.includes('--json')
const files = walk(SRC).filter((f) => inScope(relative(SRC, f).split('\\').join('/')))

const offenders = []
for (const file of files) {
  const rel = relative(SRC, file).split('\\').join('/')
  const source = readFileSync(file, 'utf-8')
  const lines = source.split('\n')
  for (const lineNo of unitLiteralHits(rel, source)) {
    offenders.push({ rel, line: lineNo, text: (lines[lineNo - 1] ?? '').trim().slice(0, 120) })
  }
}

if (asJson) {
  // Equivalence harness: emit the raw finding set so it can be diffed against any
  // other implementation of the same predicate.
  console.log(JSON.stringify({ files: files.length, count: offenders.length, offenders }, null, 1))
  process.exit(0)
}

const scope = computeScope()
const introduced = []
const grew = []

if (scope) {
  for (const rel of scope.touched) {
    const written = scope.written.get(`${PREFIX}${rel}`)
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
  for (const rel of scope.touched) {
    const now = hitsNow(rel).length
    const base = scope.readBase(rel)
    const then = base === null ? 0 : unitLiteralHits(rel, base).length
    if (now > then) grew.push(`  ${rel}: ${then} → ${now}`)
  }

  console.log(`[added-lines] ${introduced.length} number+unit literal(s) on lines you wrote`)
  console.log(`[vs-base] ${grew.length} touched file(s) gained number+unit literals`)
}

console.log(`OK: ${offenders.length} un-migrated number+unit literal(s) across ${files.length} `
  + `file(s), baseline ${BASELINE}.`)
if (!scope) console.log('note: I18N_BASE_REF unset; the diff-scoped checks did not run.')

if (introduced.length) {
  console.error(`\n[added-lines] FAIL: ${introduced.length} on lines this branch wrote. There is\n`
    + `no baseline to raise for these; ${BASELINE} covers only the frozen debt on lines\n`
    + `nobody touched.\n\n${ADVICE}\n\n  ${introduced.slice(0, 15).join('\n  ')}`)
}
if (grew.length) {
  console.error(`\n[vs-base] FAIL: ${grew.length} file(s) you touched hold MORE than at the base.\n`
    + `Measured live against the base ref, so re-snapshotting cannot clear it.\n\n${ADVICE}\n\n`
    + grew.join('\n'))
}

// The ceiling REPORTS; it does not fail. It covers only lines nobody touched, which no
// branch can add to; [added-lines] catches those at zero tolerance. The gate table
// requires a whole-repo count that is not a hard zero to be informational, or one
// branch's inherited debt fails another branch's build.
if (offenders.length > BASELINE) {
  console.log(`\nnote: whole-repo count ${offenders.length} is over the frozen baseline `
    + `${BASELINE}. Not failing this step; the diff-scoped checks above are the gate.`)
}

process.exit(introduced.length || grew.length ? 1 : 0)
