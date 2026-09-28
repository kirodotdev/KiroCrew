#!/usr/bin/env node
/**
 * check-restyle-ratchet.mjs — keep the `shadcn/no-restyle` backlog from growing.
 *
 * `shadcn/no-restyle` reports a `className` that changes what a `ui/` primitive
 * owns — its color, its padding, its shape (`<DropdownMenuItem className=
 * "bg-red-500">`). The tree carries a few hundred such call sites, and the
 * eslint gate runs `--max-warnings 0`, so the rule stays off in
 * `eslint.config.js` and nothing stops the backlog from growing while the
 * migration (fix the sites, or write per-component `contracts`) waits on a
 * design decision.
 *
 * ## A per-file ceiling, not a hard zero
 *
 * This gate runs the rule through its own in-memory ESLint config — the same
 * parser and plugin `eslint.config.js` uses, `allow: ['layout']` so margins and
 * widths still pass — tallies the findings per file, and compares each count
 * against `restyle-baseline.json` beside this script. The baseline is the
 * ceiling: a file whose count RISES fails, a file absent from the baseline with
 * any finding fails, and a file whose count FELL fails too, naming the command
 * that records the drop — a fall that is merely printed is progress nobody
 * locked in, and the file can grow back to its old number unnoticed. Nothing
 * else is charged to the change: the backlog is frozen at the recorded numbers,
 * and the recorded numbers can only go down.
 *
 * Per file rather than one total on purpose. A single number is the shape the
 * i18n gates learned to avoid (see the comment on that step in ci.yml): another
 * branch can move it, and then the failure names no diff anyone can fix. A
 * per-file count is local to the file the change touched.
 *
 * ## The baseline only shrinks
 *
 * `--update-baseline` rewrites the file taking `min(recorded, current)` for
 * every entry and dropping entries that reached 0. It never raises a number and
 * never adds a file, so running it cannot absorb a regression. The two edits it
 * cannot make are deliberate and are made by hand, where the PR diff shows them:
 *
 *   - a file was MOVED: move its entry to the new path (the count may not grow);
 *   - the file is missing (first run, or deleted): `--update-baseline` seeds it
 *     from the current tree and says so. Deleting a committed baseline is
 *     therefore a reviewable event, not a silent one — the gate itself fails
 *     closed while the file is absent.
 *
 * ## Why not ESLint's own bulk suppressions
 *
 * `eslint --suppress-rule shadcn/no-restyle` writes the same per-file count
 * ceiling to `eslint-suppressions.json`, and the CLI fails on excess and on
 * unused entries. Two measured gaps keep the rule out of eslint.config.js:
 *
 *   1. Only the CLI applies that file. The Node API (`new ESLint().lintFiles`)
 *      reports every suppressed error — measured: 6 of 6 on RepoSwitcher.tsx
 *      with the suppressions file present — and editors integrate through the
 *      Node API, so the rule at `error` puts a few hundred red marks in front
 *      of every contributor while CI stays green.
 *   2. The CLI loads `eslint-suppressions.json` from the cwd for EVERY config.
 *      `check-i18n-strings.mjs` drives a second CLI run with
 *      eslint.i18n.config.js, where no `no-restyle` finding exists, so every
 *      entry is "unused" and that run exits 2 — measured — unless it grows a
 *      `--pass-on-unpruned-suppressions` it has no other reason to carry.
 *
 * A separate script with its own config has neither problem, which is also
 * why check-phantom-classes.mjs and the i18n gates run outside `npm run lint`.
 *
 * ## Usage
 *
 *     # gate: exit 1 if any file's count rose, fell, or a new file has findings
 *     npm run lint:restyle-ratchet
 *
 *     # record progress: lower counts, prune zeros (never raises, never adds)
 *     npm run lint:restyle-ratchet -- --update-baseline
 *
 *     # self-test: the rule fires on a planted restyle and the comparison
 *     # answers each case the way this header says it does
 *     npm run lint:restyle-ratchet -- --test
 *
 * Runs whole-tree with no base ref: the baseline is committed, so the gate is
 * complete for regression without a diff.
 */
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { join, relative } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { ESLint } from 'eslint'
import tsParser from '@typescript-eslint/parser'
import { plugin as shadcn } from '@shadcn/lint'

const WEBSITE = join(fileURLToPath(new URL('.', import.meta.url)), '..')
const REPO_ROOT = join(WEBSITE, '..')
const BASELINE = join(WEBSITE, 'scripts', 'restyle-baseline.json')
const BASELINE_REL = 'website/scripts/restyle-baseline.json'
const RULE = 'shadcn/no-restyle'
const GATE = 'restyle ratchet'
const UPDATE_CMD = 'npm run lint:restyle-ratchet -- --update-baseline'

/** The one config the gate lints with. Kept in step with the `shadcn` block of
 *  eslint.config.js by hand: same parser options, same plugin instance (which
 *  reads components.json from the website root, so `cwd` below matters), and
 *  the same two carve-outs the other shadcn rules have there — component
 *  implementations compose their own classes (the rule's docs prescribe this
 *  override), and tests pass throwaway class names that render to no one. */
const CONFIG = [
  {
    files: ['src/**/*.{ts,tsx}'],
    languageOptions: {
      parser: tsParser,
      parserOptions: {
        ecmaVersion: 2020,
        sourceType: 'module',
        ecmaFeatures: { jsx: true },
      },
    },
    plugins: { shadcn },
    // Directives in the tree address the real config's rules, not this one.
    linterOptions: { reportUnusedDisableDirectives: 'off' },
    rules: {
      [RULE]: ['error', { allow: ['layout'] }],
    },
  },
  {
    files: [
      'src/components/ui/**/*.{ts,tsx}',
      'src/**/*.test.{ts,tsx}',
      'src/test/**/*.{ts,tsx}',
    ],
    rules: { [RULE]: 'off' },
  },
]

const linter = () =>
  new ESLint({ cwd: WEBSITE, overrideConfigFile: true, overrideConfig: CONFIG })

/** Repo-relative, forward-slash path — the baseline's key. */
const relKey = (abs) => relative(REPO_ROOT, abs).split('\\').join('/')

// ---------------------------------------------------------------------------
// Counting
// ---------------------------------------------------------------------------

/** `{ [file]: count }` over every lint result, plus the files ESLint could not
 *  parse. A fatal message means the file's sites were never counted, so the
 *  caller must fail rather than read the missing count as zero. */
function tally(results) {
  const counts = {}
  const unparsed = []
  for (const r of results) {
    const key = relKey(r.filePath)
    if (r.messages.some((m) => m.fatal)) unparsed.push(key)
    const n = r.messages.filter((m) => m.ruleId === RULE).length
    if (n > 0) counts[key] = n
  }
  return { counts, unparsed }
}

async function scanTree() {
  const results = await linter().lintFiles(['src/**/*.{ts,tsx}'])
  return tally(results)
}

// ---------------------------------------------------------------------------
// Baseline
// ---------------------------------------------------------------------------

/** Parse the baseline, or throw with the reason. Shape is `{ "<file>": <n> }`
 *  with every `n` a positive integer: a zero or a non-number would be an entry
 *  the ratchet cannot reason about, so it is refused rather than coerced. */
function parseBaseline(text) {
  let data
  try {
    data = JSON.parse(text)
  } catch (e) {
    throw new Error(`not valid JSON (${e.message})`)
  }
  if (data === null || typeof data !== 'object' || Array.isArray(data)) {
    throw new Error('expected an object of { "<file>": <count> }')
  }
  for (const [file, n] of Object.entries(data)) {
    if (!Number.isInteger(n) || n <= 0) {
      throw new Error(`"${file}": count must be a positive integer, got ${JSON.stringify(n)}`)
    }
  }
  return data
}

function readBaseline() {
  return parseBaseline(readFileSync(BASELINE, 'utf-8'))
}

/** Sorted keys, two-space indent, trailing newline — one canonical spelling so
 *  a regenerated file diffs only where a number moved. */
function serializeBaseline(counts) {
  const sorted = Object.fromEntries(
    Object.keys(counts)
      .sort()
      .map((k) => [k, counts[k]]),
  )
  return JSON.stringify(sorted, null, 2) + '\n'
}

/** Compare current counts with the recorded ceiling.
 *
 *  - `grown`   — recorded file whose count rose: `{ file, from, to }`
 *  - `added`   — file with findings and no entry: `{ file, to }`
 *  - `shrunk`  — recorded file whose count fell (or vanished): `{ file, from, to }`
 *
 *  All three fail the gate; `shrunk` is the one the author fixes by recording,
 *  not by editing code. */
function compare(baseline, current) {
  const grown = []
  const added = []
  const shrunk = []
  for (const [file, to] of Object.entries(current)) {
    if (!(file in baseline)) added.push({ file, to })
    else if (to > baseline[file]) grown.push({ file, from: baseline[file], to })
  }
  for (const [file, from] of Object.entries(baseline)) {
    const to = current[file] ?? 0
    if (to < from) shrunk.push({ file, from, to })
  }
  const byFile = (a, b) => (a.file < b.file ? -1 : a.file > b.file ? 1 : 0)
  return { grown: grown.sort(byFile), added: added.sort(byFile), shrunk: shrunk.sort(byFile) }
}

/** The baseline after `--update-baseline`: `min(recorded, current)` per
 *  recorded file, zeros dropped, nothing added. */
function shrink(baseline, current) {
  const next = {}
  for (const [file, from] of Object.entries(baseline)) {
    const n = Math.min(from, current[file] ?? 0)
    if (n > 0) next[file] = n
  }
  return next
}

// ---------------------------------------------------------------------------
// Reporting
// ---------------------------------------------------------------------------

const total = (counts) => Object.values(counts).reduce((a, b) => a + b, 0)

const REMEDY =
  `\nA className on a ui/ primitive changed something the primitive owns (its ` +
  `color, spacing, shape or typography). Use the component's own variant or ` +
  `size prop, put layout classes (margin, width, flex) on the call site and the ` +
  `rest in the component file, or wrap the primitive in a small named ` +
  `component that carries the class. The baseline records the count each file ` +
  `had before this change and can only go down; see website/docs/` +
  `frontend-conventions.md § Styling.`

function report(baseline, current) {
  const { grown, added, shrunk } = compare(baseline, current)
  const files = Object.keys(current).length
  console.log(
    `${GATE}: ${total(current)} restyle site(s) in ${files} file(s); baseline ` +
      `records ${total(baseline)} in ${Object.keys(baseline).length}`,
  )
  if (shrunk.length) {
    console.log(
      `::error::${GATE}: ${shrunk.length} file(s) now have fewer restyle sites ` +
        `than the baseline records. Record the progress so the ceiling cannot ` +
        `drift back up: ${UPDATE_CMD}`,
    )
    for (const s of shrunk) console.log(`  ${s.file}: ${s.from} -> ${s.to}`)
  }
  if (grown.length || added.length) {
    console.log(
      `::error::${GATE}: ${grown.length + added.length} file(s) restyle more ui/ ` +
        `primitives than ${BASELINE_REL} allows:`,
    )
    for (const g of grown) console.log(`  ${g.file}: ${g.from} -> ${g.to}`)
    for (const a of added) console.log(`  ${a.file}: (not in baseline) -> ${a.to}`)
    console.log(REMEDY)
  }
  if (grown.length || added.length || shrunk.length) return 1
  console.log(`${GATE}: every file matches the baseline \u2713`)
  return 0
}

function reportUnparsed(unparsed) {
  console.log(
    `::error::${GATE}: ESLint could not parse these files, so their restyle ` +
      `sites were never counted: ${unparsed.join(', ')}`,
  )
  return 1
}

// ---------------------------------------------------------------------------
// Modes
// ---------------------------------------------------------------------------

async function gate() {
  if (!existsSync(BASELINE)) {
    console.log(
      `::error::${GATE}: ${BASELINE_REL} is missing, so there is no ceiling to ` +
        `hold. Seed it from the current tree with: ${UPDATE_CMD}`,
    )
    return 1
  }
  let baseline
  try {
    baseline = readBaseline()
  } catch (e) {
    console.log(`::error::${GATE}: ${BASELINE_REL} is unusable — ${e.message}`)
    return 1
  }
  const { counts, unparsed } = await scanTree()
  if (unparsed.length) return reportUnparsed(unparsed)
  return report(baseline, counts)
}

async function updateBaseline() {
  const { counts, unparsed } = await scanTree()
  if (unparsed.length) return reportUnparsed(unparsed)
  if (!existsSync(BASELINE)) {
    writeFileSync(BASELINE, serializeBaseline(counts))
    console.log(
      `${GATE}: seeded ${BASELINE_REL} with ${total(counts)} restyle site(s) in ` +
        `${Object.keys(counts).length} file(s)`,
    )
    return 0
  }
  const baseline = readBaseline()
  const next = shrink(baseline, counts)
  const { grown, added } = compare(baseline, counts)
  writeFileSync(BASELINE, serializeBaseline(next))
  const pruned = Object.keys(baseline).length - Object.keys(next).length
  const lowered = Object.keys(next).filter((f) => next[f] < baseline[f]).length
  console.log(
    `${GATE}: pruned ${pruned} entr(y|ies), lowered ${lowered}; ` +
      `${Object.keys(next).length} file(s) / ${total(next)} site(s) remain`,
  )
  if (grown.length || added.length) {
    console.log(
      `::notice::${GATE}: ${grown.length + added.length} file(s) exceed the ` +
        `ceiling and were NOT recorded — the baseline never rises. The gate ` +
        `still fails on them.`,
    )
  }
  return 0
}

// ---------------------------------------------------------------------------
// Self-test — the rule is live, and the comparison keeps its promises
// ---------------------------------------------------------------------------

const FIXTURE =
  `import { DropdownMenuItem } from '@/components/ui/dropdown-menu'\n` +
  `export const Restyled = () => <DropdownMenuItem className="bg-red-500">x</DropdownMenuItem>\n` +
  `export const Layout = () => <DropdownMenuItem className="mt-2 w-full">x</DropdownMenuItem>\n`

const COMPARE_PROBES = [
  {
    name: 'a count that rose is grown',
    baseline: { a: 2 },
    current: { a: 3 },
    want: { grown: ['a'], added: [], shrunk: [] },
  },
  {
    name: 'a file the baseline does not know is added',
    baseline: { a: 2 },
    current: { a: 2, b: 1 },
    want: { grown: [], added: ['b'], shrunk: [] },
  },
  {
    name: 'a count that fell, or a file that vanished, is shrunk',
    baseline: { a: 2, c: 4 },
    current: { a: 1 },
    want: { grown: [], added: [], shrunk: ['a', 'c'] },
  },
  {
    name: 'equal counts are nothing',
    baseline: { a: 2 },
    current: { a: 2 },
    want: { grown: [], added: [], shrunk: [] },
  },
]

async function selfTest() {
  const disagree = []
  for (const p of COMPARE_PROBES) {
    const got = compare(p.baseline, p.current)
    for (const k of ['grown', 'added', 'shrunk']) {
      const files = got[k].map((x) => x.file).join(',')
      if (files !== p.want[k].join(',')) {
        disagree.push(`${p.name}: ${k} expected [${p.want[k].join(', ')}], got [${files}]`)
      }
    }
  }
  // shrink: lowers, prunes, never raises, never adds.
  const shrunk = shrink({ a: 3, b: 1, c: 2 }, { a: 1, b: 0, c: 5, d: 2 })
  if (JSON.stringify(shrunk) !== JSON.stringify({ a: 1, c: 2 })) {
    disagree.push(`shrink: expected {a:1,c:2}, got ${JSON.stringify(shrunk)}`)
  }
  // parseBaseline refuses the shapes the ratchet cannot reason about.
  for (const bad of ['[]', '{"a": 0}', '{"a": "2"}', '{"a": 1.5}', 'nope']) {
    let threw = false
    try {
      parseBaseline(bad)
    } catch {
      threw = true
    }
    if (!threw) disagree.push(`parseBaseline: accepted ${bad}`)
  }
  if (serializeBaseline({ b: 1, a: 2 }) !== '{\n  "a": 2,\n  "b": 1\n}\n') {
    disagree.push('serializeBaseline: keys not sorted or format drifted')
  }
  // Floor: the rule must be live under this config. Without it every count
  // above would be zero and the gate would pass everything.
  const [res] = await linter().lintText(FIXTURE, { filePath: join(WEBSITE, 'src', '__restyle_probe__.tsx') })
  const lines = res.messages.filter((m) => m.ruleId === RULE).map((m) => m.line)
  if (lines.join(',') !== '2') {
    disagree.push(
      `rule: expected ${RULE} to fire on line 2 only (bg-red-500, not the ` +
        `layout classes on line 3), got lines [${lines.join(', ')}]`,
    )
  }
  if (disagree.length) {
    console.log(`::error::${GATE} self-test: ${disagree.length} probe(s) disagree:`)
    for (const d of disagree) console.log(`  ${d}`)
    return 1
  }
  console.log(`${GATE} self-test: ${COMPARE_PROBES.length + 4} probes agree \u2713`)
  return 0
}

// ---------------------------------------------------------------------------

async function main(argv) {
  if (argv.includes('--test')) return selfTest()
  if (argv.includes('--update-baseline')) return updateBaseline()
  return gate()
}

// Run only as a program: an `import` of this module (a future unit test) must
// not lint the tree and call `process.exit` mid-collection.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.exit(await main(process.argv.slice(2)))
}
