#!/usr/bin/env node
/**
 * Untranslated-string gate. Two checks with different jobs:
 *
 * 1. **Lines this branch wrote — ZERO tolerance, no ledger.** A user-visible literal on
 *    an added line fails, whatever any committed number says. This is the real gate.
 * 2. **Per-file ceilings — UPWARD-ONLY.** `untranslated-baseline.json` holds the frozen
 *    debt on lines nobody touched. Going over a ceiling fails; going under does not.
 *
 * **Per file.** A single total lets three strings fixed in one file pay for three
 * added in another. Per-file counts make that visible, and turn the baseline into a
 * worklist: `Phase 1` is "drive these files to zero", which can be sliced into
 * reviewable PRs by file.
 *
 * ## Why the ledger stopped being the enforcement mechanism
 *
 * The gate shipped bidirectional: a count that dropped *below* its baseline also failed,
 * so whoever fixed a string had to commit the smaller number and the gain could not be
 * given back. Two problems, in increasing order of seriousness.
 *
 * The first is contention. This baseline is one generated artifact holding a `_total`
 * plus 269 per-file entries, regenerated from a whole-tree scan. Forcing every improving
 * change to re-snapshot it means two branches that fix strings in *different* files
 * still conflict textually, in `_total` if nowhere else, and every merge to `main`
 * invalidates the number in every other open branch — serialisation imposed by the
 * ledger, not by the code being changed.
 *
 * The second is that the downward guard did not actually prevent regressions. It
 * required an *edit*, and the edit is one command. `195904c` shipped a new app with 113
 * untranslated strings and moved `_total` from 1747 to 1860 inside a 5,942-line diff,
 * under the fully bidirectional gate, and CI was green — because re-snapshotting makes
 * the live count agree again and the hunk reads like routine ledger churn. A guard whose
 * bypass is `--update` protects only against forgetting.
 *
 * So the strict check moved to the diff, where there is no number to regenerate, and the
 * ceilings became upward-only. That is a net tightening: `195904c` fails check 1 today.
 *
 * **The ultimate goal is zero for every file here.** A ceiling of 0 has nothing left to
 * decrease, so "only an increase fails" IS the strict gate; when the last file reaches 0
 * this baseline is deleted and the check becomes an unconditional "no untranslated
 * strings". Phase 1's stated goal was always "drive these files to zero" — the
 * relaxation only removes the obligation to re-snapshot on the way there. Tracked in
 * issue #1004.
 *
 * Residual cost, stated: a ceiling drifts above reality between snapshots, so an
 * UNTOUCHED file could in principle give a fixed string back through some path that
 * writes no new line. Check 1 covers everything a branch writes; nothing covers a file
 * nobody edits, which is also a file nobody can regress.
 *
 * Usage:
 *   node scripts/check-i18n-strings.mjs            # gate
 *   node scripts/check-i18n-strings.mjs --update    # re-snapshot the ceilings
 *   I18N_BASE_REF=origin/main node scripts/check-i18n-strings.mjs
 */

import { execFileSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ALL_CAPS_MARKER } from '../eslint-rules/i18n-strict.js'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const BASELINE = path.join(ROOT, 'src/i18n/untranslated-baseline.json')
const STRICT_BASELINE = path.join(ROOT, 'src/i18n/untranslated-strict-baseline.json')

const STRICT_BASELINE_COMMENT =
  'Untranslated user-visible strings that live INSIDE an ALL-CAPS module constant, '
  + 'which `eslint-plugin-i18next` exempts by default (see eslint-rules/i18n-strict.js). '
  + 'One aggregate, not per-file: `[added-lines]` already fails at zero tolerance on any '
  + 'of these on a line you wrote, which is what licenses an upward-only count '
  + '(website/AGENTS.md, ratchet rule). Per-file would recreate the cross-branch conflict '
  + 'the shared ledger has, for no extra enforcement. UPWARD-ONLY, and the goal is 0: at '
  + 'zero this class is gone and both this file and the exemption can be deleted. '
  + 'Re-snapshot with `node scripts/check-i18n-strings.mjs --update`.'

const BASELINE_COMMENT =
  'Untranslated user-visible strings, per file. Generated — re-snapshot with '
  + '`node scripts/check-i18n-strings.mjs --update`. These are CEILINGS, not current '
  + 'counts: the gate fails only when a file gains strings, so a number here may sit '
  + 'above reality until someone re-snapshots. Also the Phase 1 worklist: drive files '
  + 'to zero, one PR per file or per directory.'

/**
 * Classification, so the baseline says what kind of work each file holds rather
 * than only how much. The categories map onto how a string gets fixed:
 * `expression` and `prose` are local edits, `template` and `object-prop` need the
 * value's shape to change first.
 */
export function classify(text) {
  const t = text.replace(/^disallow literal string: /, '')
  if (/`/.test(t)) return 'template'
  if (/^\[/.test(t)) return 'array'
  if (/^(msg|message|error|title|label|description|placeholder|hint|summary|name|group|source):/.test(t)) return 'object-prop'
  if (/^(setError|setStatus|setMsg|notify|toast|alert|confirm)\(/.test(t)) return 'status-call'
  if (/^(aria-label|title|placeholder|alt)=/.test(t)) return 'attribute'
  if (/\?[^:]*:/.test(t)) return 'expression'
  return 'prose'
}

/**
 * Compare live per-file counts against the baseline.
 *
 * `grew` is the failing set. `shrank` is reported for visibility only — it is the
 * direction that used to fail, and the reason this function is exported and tested
 * rather than inlined: the asymmetry is the contract, so it needs coverage that does
 * not require running eslint over the whole tree.
 *
 * A file absent from the baseline has an implicit ceiling of 0, so a newly added
 * file carrying untranslated strings fails. A file that disappears from the live
 * report counts as shrunk to zero.
 */
export function diffAgainstBaseline(byFile, baseFiles) {
  const grew = []
  const shrank = []
  const seen = new Set()
  for (const [file, { total: now }] of Object.entries(byFile)) {
    seen.add(file)
    const then = baseFiles[file]?.total ?? 0
    if (now > then) grew.push(`  ${file}: ${then} → ${now}`)
    else if (now < then) shrank.push(`  ${file}: ${then} → ${now}`)
  }
  for (const [file, { total: then }] of Object.entries(baseFiles)) {
    if (!seen.has(file) && then > 0) shrank.push(`  ${file}: ${then} → 0`)
  }
  return { grew, shrank: shrank.sort() }
}

/**
 * Marks a file whose every line is attributable to this branch. Git produces no hunk
 * for an untracked file, so a new file written but not yet committed would otherwise be
 * exempt from the diff-scoped gate — precisely the case a developer is most likely to
 * be running the gate to check.
 */
export const ALL_LINES = 'all'

/**
 * Line numbers this branch ADDED, per file, parsed from `git diff -U0` hunk headers.
 *
 * `@@ -12,3 +12,4 @@` — the `+` side is `start,count`, count omitted meaning 1, and
 * `+0,0` meaning the file was deleted on this side. Only the `+` side matters: a line
 * the branch did not write cannot be a string the branch introduced.
 */
export function parseAddedLines(diff) {
  const byFile = {}
  let current = null
  for (const line of diff.split('\n')) {
    const plus = /^\+\+\+ b\/(.+)$/.exec(line)
    if (plus) {
      current = plus[1]
      byFile[current] = byFile[current] ?? new Set()
      continue
    }
    if (line.startsWith('--- ')) continue
    const hunk = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line)
    if (!hunk || current === null) continue
    const start = Number(hunk[1])
    const count = hunk[2] === undefined ? 1 : Number(hunk[2])
    for (let n = start; n < start + count; n += 1) byFile[current].add(n)
  }
  return byFile
}

/**
 * Untranslated strings sitting on a line this branch wrote.
 *
 * This is the gate that replaced the baseline's downward guard, and it is strictly
 * stronger than what it replaced. The old rule was "the committed number must equal
 * the live count", which sounds like it blocks regressions and does not: the number is
 * regenerated by one command, so admitting new debt costs `--update` and a diff hunk
 * that reads like routine ledger churn. `195904c` did exactly that — it shipped a new
 * app with 113 untranslated strings and moved `_total` from 1747 to 1860 inside a
 * 5,942-line diff, under the fully bidirectional gate, and CI was green.
 *
 * Anchoring to the diff removes the escape hatch, because there is no number to
 * regenerate: a literal on a line you wrote fails, whatever any ledger says. Frozen
 * debt on lines you did not touch is untouched, which is what keeps this from failing
 * every branch on arrival.
 *
 * Stated cost: moving an existing untranslated line counts as writing it, so code
 * motion has to wrap what it moves. That is the intended reading of "never hardcode a
 * user-facing string", and the ultimate goal for every one of these counts is zero.
 */
export function findingsOnAddedLines(report, addedByFile, { prefix = 'website/src/' } = {}) {
  const out = []
  for (const file of report) {
    // `rel` is relative to `website/src`; git reports paths from the repo root. The
    // join is explicit and tested, because getting it wrong makes this gate report
    // zero findings forever and look like it is passing.
    const added = addedByFile[`${prefix}${file.rel}`]
    if (added === undefined) continue
    // `'all'` marks a file git cannot produce a hunk for — an untracked new file. Every
    // line in it was written by this branch.
    if (added !== ALL_LINES && added.size === 0) continue
    for (const m of file.messages) {
      // A finding SPANS lines: eslint reports a multi-line template literal at its
      // opening line, so editing the third line of a five-line literal touches none of
      // the lines the finding is anchored to. Match anywhere in the range, or the shape
      // most likely to hide a rewritten user-visible string — a long interpolated
      // template — is the shape that escapes.
      const touched = added === ALL_LINES
        || rangeOf(m).some((n) => added.has(n))
      if (touched) out.push({ file: file.rel, line: m.line, message: m.message })
    }
  }
  return out
}

/** Every line a finding covers, inclusive. `endLine` is absent for a single-line report. */
function rangeOf(m) {
  const end = typeof m.endLine === 'number' && m.endLine >= m.line ? m.endLine : m.line
  const lines = []
  for (let n = m.line; n <= end; n += 1) lines.push(n)
  return lines
}

// Resolve eslint's own JS entry point and run it with `process.execPath`, NOT via `npx`.
// `execFileSync` does not go through a shell, and on Windows the installed shim is
// `npx.cmd` rather than `npx`, so `execFileSync('npx', …)` raises ENOENT — which lands
// in the catch below with an empty stdout and exits 2, aborting a REQUIRED gate on every
// Windows machine. Using `process.execPath` needs no shim and no `shell: true` (which
// would reintroduce quoting/injection concerns), and it pins the gate to the same Node
// that is already running this script.
const ESLINT_BIN = path.join(ROOT, 'node_modules', 'eslint', 'bin', 'eslint.js')

/** Run the i18n eslint config and fold its report into per-file counts. */
function scan() {
  let raw
  try {
    if (!fs.existsSync(ESLINT_BIN)) {
      console.error(
        `cannot find eslint at ${path.relative(ROOT, ESLINT_BIN)} — run \`npm ci\` in website/ first.`,
      )
      process.exit(2)
    }
    raw = execFileSync(
      process.execPath,
      [
        ESLINT_BIN,
        'src',
        // The STRICT config, and the only full-tree pass. Its findings carry a
        // marker when they exist only because the wrapper looked inside an
        // ALL-CAPS constant, so this one run yields BOTH populations: the
        // upstream-visible strings that the per-file ceilings measure, and the
        // strict-only debt that upstream hid. Running eslint twice to separate
        // them would double the slowest step in `i18n:check`.
        '--config', 'eslint.i18n.strict.config.js',
        '--no-inline-config',
        '--format', 'json',
      ],
      { cwd: ROOT, encoding: 'utf-8', maxBuffer: 64 * 1024 * 1024 },
    )
  } catch (err) {
    // eslint exits non-zero whenever it reports anything, which is the normal case
    // here. Its stdout is still the report; only an empty stdout is a real failure.
    raw = err.stdout || ''
    if (!raw.trim()) {
      console.error('eslint produced no output:\n', err.stderr || err.message)
      process.exit(2)
    }
  }

  const byFile = {}
  const perFile = []
  let strictOnly = 0
  const strictOnlyByFile = {}
  for (const file of JSON.parse(raw)) {
    if (file.messages.length === 0) continue
    const rel = path.relative(path.join(ROOT, 'src'), file.filePath).split(path.sep).join('/')
    const counts = {}
    // Split, then classify: a marked finding is one the ceilings never counted, so
    // folding it into a per-file ceiling would be the bulk re-snapshot this design
    // exists to avoid.
    const upstreamVisible = []
    for (const m of file.messages) {
      if (m.message.endsWith(ALL_CAPS_MARKER)) {
        strictOnly += 1
        strictOnlyByFile[rel] = (strictOnlyByFile[rel] || 0) + 1
        continue
      }
      upstreamVisible.push(m)
    }
    for (const m of upstreamVisible) {
      const c = classify(m.message)
      counts[c] = (counts[c] || 0) + 1
    }
    if (upstreamVisible.length > 0) {
      byFile[rel] = { total: upstreamVisible.length, ...Object.fromEntries(Object.entries(counts).sort()) }
    }
    // Line numbers are kept, not just counts: the diff-scoped gate needs to know WHICH
    // lines each finding covers, and re-running eslint to get them would double the
    // slowest step in `i18n:check`. `endLine` matters as much as `line` — a multi-line
    // template literal is reported at its opening line only.
    perFile.push({
      rel,
      messages: file.messages.map((m) => ({ line: m.line, endLine: m.endLine, message: m.message })),
    })
  }
  return { byFile, perFile, strictOnly, strictOnlyByFile }
}

/**
 * Files this branch touched, and which of their lines it wrote.
 *
 * Returns `null` only when there is genuinely no base to compare against — a push to
 * `main`, where `I18N_BASE_REF` is empty. If a base ref IS configured and cannot be
 * used, this exits non-zero rather than returning `null`: `ci.yml` records having
 * watched the sibling gate skip itself green on a failed fetch, and a gate that cannot
 * run must fail, not pass.
 */
function diffScope() {
  const baseRef = process.env.I18N_BASE_REF
  if (!baseRef) return null
  const cwd = path.resolve(ROOT, '..')
  const git = (args) => execFileSync('git', args, { cwd, encoding: 'utf-8', maxBuffer: 128 * 1024 * 1024 })

  try {
    git(['rev-parse', '--verify', `${baseRef}^{commit}`])
  } catch {
    console.error(
      `cannot resolve ${baseRef}. The diff-scoped i18n gate needs the base ref; fetch it\n`
      + 'before running this script, or unset I18N_BASE_REF to check only the ceilings.',
    )
    process.exit(2)
  }

  // Prefer the merge base: it keeps commits that landed on the base branch after this
  // one forked from being attributed to this branch. CI checks out at depth 1 and
  // fetches the base ref at depth 1 too, so there is no shared history there and
  // `merge-base` fails — fall back to the base tip, which needs only the two trees.
  let from
  try {
    from = git(['merge-base', baseRef, 'HEAD']).trim()
  } catch {
    from = baseRef
  }

  // The WORKING TREE is the right-hand side, not `HEAD`. `base...HEAD` compares commits,
  // so it cannot see uncommitted work — which would exempt exactly the moment a
  // developer runs this gate locally to check what they just wrote.
  const added = parseAddedLines(git(['diff', '-U0', '--no-color', from, '--', 'website/src']))

  // Untracked files produce no diff hunk, so add them explicitly. On CI the tree is
  // clean and this is empty; locally it is the difference between the gate seeing a
  // file you just created and silently passing it.
  const untracked = git(['ls-files', '--others', '--exclude-standard', '--', 'website/src'])
  for (const rel of untracked.split('\n').filter(Boolean)) added[rel] = ALL_LINES

  const readBase = (repoRel) => {
    try {
      return git(['show', `${from}:${repoRel}`])
    } catch {
      // Absent at the base: the file is new on this branch, so its base count is zero.
      return null
    }
  }
  return { from, added, readBase }
}

/**
 * Untranslated-string counts for the same files, as they were at the base.
 *
 * `[added-lines]` attributes a finding to a line, and eslint anchors a finding to the
 * LITERAL. So an edit that changes the surrounding context — `console.log(` to
 * `setStatus(`, on its own line, with the string on the next — converts an exempt site
 * into a real one without touching the line the finding sits on. Line attribution
 * cannot see that by construction.
 *
 * Comparing the COUNT PER FILE against the base can: the file gained a finding, so the
 * number went up, and the base number is computed live rather than read from a ledger
 * that may be stale. `lintText` with the real `filePath` resolves the same config and
 * produces the same messages as `lintFiles` (verified: 7 and 7 on `api/helpers.ts`), so
 * this measures the same population as the main scan.
 *
 * Only touched files are linted, so the cost is proportional to the diff.
 */
async function baseCounts(scope, files) {
  const engine = await strictEngine()
  const counts = {}
  for (const rel of files) {
    const source = scope.readBase(`website/src/${rel}`)
    if (source === null) {
      counts[rel] = 0
      continue
    }
    const results = await engine.lintText(source, {
      filePath: path.join(ROOT, 'src', rel),
      warnIgnored: false,
    })
    counts[rel] = results[0] ? results[0].messages.length : 0
  }
  return counts
}

/**
 * The eslint engine the DIFF-SCOPED gates run: same options as the ceiling scan,
 * plus the ALL-CAPS declarator hole closed (`eslint.i18n.strict.config.js`).
 *
 * The split is deliberate and documented in that file: the ceilings keep the
 * upstream rule so the committed ledger stays valid, while the gates that have no
 * ledger — `[added-lines]` at zero tolerance, and `[vs-base]` computed live on
 * both sides — hold every line a branch writes to the stricter standard. Without
 * this, a module-level ALL-CAPS label table is invisible to every gate, which is
 * how `ChatSidebar`'s filter/sort menus and `lib/effort.ts`'s level labels shipped
 * untranslated in ten locales while their files reported clean.
 */
async function strictEngine() {
  const { ESLint } = await import('eslint')
  return new ESLint({
    cwd: ROOT,
    overrideConfigFile: 'eslint.i18n.strict.config.js',
    // Match the main scan, which passes `--no-inline-config`: every `eslint-disable` in
    // this tree targets the OTHER config's rules, and honouring them here reports each
    // as a problem.
    allowInlineConfig: false,
  })
}

/** Files that gained untranslated strings relative to their own state at the base. */
export function grewVersusBase(headCounts, baseCountsByFile) {
  const grew = []
  for (const rel of Object.keys(baseCountsByFile).sort()) {
    const now = headCounts[rel] ?? 0
    const then = baseCountsByFile[rel]
    if (now > then) grew.push(`  ${rel}: ${then} → ${now}`)
  }
  return grew
}

async function main() {
  const update = process.argv.includes('--update')
  const { byFile, perFile, strictOnly, strictOnlyByFile } = scan()
  const total = Object.values(byFile).reduce((a, f) => a + f.total, 0)

  if (update) {
    const current = {
      _comment: BASELINE_COMMENT,
      _total: total,
      files: Object.fromEntries(Object.entries(byFile).sort(([a], [b]) => a.localeCompare(b))),
    }
    fs.writeFileSync(BASELINE, `${JSON.stringify(current, null, 2)}\n`)
    console.log(`wrote ${path.relative(ROOT, BASELINE)}: ${total} strings across ${Object.keys(byFile).length} files`)
    fs.writeFileSync(STRICT_BASELINE, `${JSON.stringify({
      _comment: STRICT_BASELINE_COMMENT,
      _total: strictOnly,
    }, null, 2)}\n`)
    console.log(
      `wrote ${path.relative(ROOT, STRICT_BASELINE)}: ${strictOnly} strings inside ALL-CAPS `
      + `constants across ${Object.keys(strictOnlyByFile).length} files`,
    )
    return
  }

  if (!fs.existsSync(BASELINE)) {
    console.error(`missing ${path.relative(ROOT, BASELINE)} — run with --update`)
    process.exit(2)
  }
  const base = JSON.parse(fs.readFileSync(BASELINE, 'utf-8'))
  const { grew, shrank } = diffAgainstBaseline(byFile, base.files)

  // The hard gates, and the ones a ledger cannot buy its way past. Checked BEFORE the
  // ceilings, because these are the findings a contributor can act on without knowing
  // anything about baselines.
  const scope = diffScope()
  if (scope === null) {
    console.log(
      '[diff-scope] skipped — I18N_BASE_REF is unset, so there is no branch to diff '
      + '(push to main). Only the per-file ceilings were checked.',
    )
  } else {
    // Both gates below count EVERY finding from the strict pass, including the ones
    // the per-file ceilings deliberately exclude: they have no ledger to raise, so
    // they are where the ALL-CAPS hole is enforced.
    const touched = Object.keys(scope.added)
      .filter((p) => p.startsWith('website/src/') && /\.tsx?$/.test(p))
      .map((p) => p.slice('website/src/'.length))
    // 1. A literal on a line this branch wrote — marked findings included, since a
    //    string inside an ALL-CAPS table is exactly what this gate exists to catch.
    const introduced = findingsOnAddedLines(perFile, scope.added)
    console.log(`[added-lines] ${introduced.length} untranslated string(s) on lines this branch wrote.`)

    // 2. A file that gained findings relative to its OWN state at the base. Catches the
    //    case line attribution cannot: an edit to the surrounding context that turns an
    //    exempt site into a real one while the literal's line is untouched.
    const grewFromBase = grewVersusBase(
      Object.fromEntries(perFile.map(({ rel, messages }) => [rel, messages.length])),
      await baseCounts(scope, touched),
    )
    console.log(`[vs-base] ${grewFromBase.length} touched file(s) gained untranslated strings vs the base.`)

    if (introduced.length > 0) {
      console.error('')
      for (const f of introduced.slice(0, 40)) {
        console.error(`  ${f.file}:${f.line}  ${f.message}`)
      }
      if (introduced.length > 40) console.error(`  … and ${introduced.length - 40} more`)
      console.error(
        `\n${introduced.length} untranslated user-visible string(s) sit on lines this branch\n`
        + 'wrote. There is no baseline to raise for these — the ceilings cover the frozen debt\n'
        + 'on lines you did not touch, not new copy.',
      )
    }

    if (grewFromBase.length > 0) {
      console.error(
        `\n${grewFromBase.length} file(s) you touched hold MORE untranslated strings than they\n`
        + `did at the base:\n${grewFromBase.join('\n')}\n\n`
        + 'Measured live against the base, not against the committed baseline, so re-snapshotting\n'
        + 'the ledger will not clear it. If the literal itself did not change, an edit around it\n'
        + 'took it out of an exemption — for example a `console.log(` that became a `setStatus(`.',
      )
    }

    if (introduced.length > 0 || grewFromBase.length > 0) {
      console.error(
        '\nWrap them with `i18nT()` (or `useTranslation`) and add the keys to the catalog. If a\n'
        + 'string is genuinely not user-visible copy, exclude it by shape in\n'
        + '`eslint.i18n.config.js` and say why.\n\n'
        + 'These two gates run `eslint.i18n.strict.config.js`, which also looks INSIDE\n'
        + 'ALL-CAPS module constants — the per-file ceilings do not, so a finding here can be\n'
        + 'one the ceiling numbers never counted. Store a catalog key in the table and\n'
        + 'translate where it renders (see `FILTER_LABEL_KEY` in pages/ChatSidebar.tsx).',
      )
      process.exit(1)
    }
  }

  if (grew.length > 0) {
    console.error(
      `${grew.length} file(s) gained untranslated strings:\n${grew.join('\n')}\n\n`
      + 'Wrap them with `i18nT()` and add the keys to the catalog. If a string is genuinely\n'
      + 'not user-visible copy, exclude it by shape in `eslint.i18n.config.js` rather than\n'
      + 'raising the baseline.',
    )
    process.exit(1)
  }

  // The class the ceilings do not cover, as its own upward-only number so it has
  // something to converge to zero. Without it, "the ledger at 0 means no
  // untranslated strings" would become false the moment the last per-file ceiling
  // is cleared — ~1100 real strings inside ALL-CAPS constants would still be there,
  // counted by nothing. Separate file, one aggregate: it conflicts with no other
  // branch, and `[added-lines]` supplies the diff-scoped strictness that licenses an
  // upward-only count at all (website/AGENTS.md, ratchet rule).
  if (!fs.existsSync(STRICT_BASELINE)) {
    console.error(`missing ${path.relative(ROOT, STRICT_BASELINE)} — run with --update`)
    process.exit(2)
  }
  const strictCeiling = JSON.parse(fs.readFileSync(STRICT_BASELINE, 'utf-8'))._total
  if (!Number.isInteger(strictCeiling)) {
    // A dropped key or a bad merge resolution would otherwise leave `undefined`
    // here, `1100 > undefined` is false, and the gate would report "at or below the
    // ceiling of undefined" and never fail again. Fail loud instead — same reason
    // `assertUpstreamShape` throws rather than degrading to an empty gate.
    console.error(
      `${path.relative(ROOT, STRICT_BASELINE)} has no integer \`_total\` (found `
      + `${JSON.stringify(strictCeiling)}). Re-snapshot it with --update; a gate that `
      + 'cannot read its own ceiling must fail, not pass.',
    )
    process.exit(2)
  }
  if (strictOnly > strictCeiling) {
    const worst = Object.entries(strictOnlyByFile).sort((a, b) => b[1] - a[1]).slice(0, 15)
    console.error(
      `${strictOnly} untranslated string(s) sit inside ALL-CAPS module constants, above the\n`
      + `ceiling of ${strictCeiling}. These are invisible to the per-file ceilings by design —\n`
      + 'store a catalog key in the table and translate where it renders (see\n'
      + '`FILTER_LABEL_KEY` in pages/ChatSidebar.tsx). Densest files:\n'
      + worst.map(([f, n]) => `  ${n}\t${f}`).join('\n'),
    )
    process.exit(1)
  }
  console.log(
    `OK: ${strictOnly} untranslated string(s) inside ALL-CAPS constants across `
    + `${Object.keys(strictOnlyByFile).length} files, at or below the ceiling of ${strictCeiling}.`,
  )

  // Deliberately NOT a failure — see the header. Re-snapshotting is optional and
  // never required to land a change, which is what keeps parallel branches from
  // colliding in this file.
  if (shrank.length > 0) {
    console.log(
      `${shrank.length} file(s) improved since the last snapshot:\n${shrank.join('\n')}\n\n`
      + 'Nothing to do. Run `node scripts/check-i18n-strings.mjs --update` if you want to\n'
      + 'tighten the ceilings now, but only when no other i18n branch is in flight — that\n'
      + 'regeneration is what makes this file conflict.',
    )
  }

  console.log(
    `OK: ${total} untranslated strings across ${Object.keys(byFile).length} files, `
    + `at or below the baseline of ${base._total ?? 'unknown'}.`,
  )
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  await main()
}
