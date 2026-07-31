#!/usr/bin/env node
/**
 * Untranslated-string gate: a PER-FILE, BIDIRECTIONAL ratchet.
 *
 * `eslint --max-warnings N` only fails when the count goes UP, and only in
 * aggregate. Both limits matter here.
 *
 * **Bidirectional.** A one-way gate has no downward pressure: nobody is obliged to
 * lower it, so the debt sits at N forever. This repo already has the evidence —
 * `ci.yml`'s own comment records its eslint budget being raised 1066 → 1116. Failing
 * when the count drops *below* the baseline forces whoever fixed a string to commit
 * the smaller number, which locks the gain in.
 *
 * **Per file.** A single total lets three strings fixed in one file pay for three
 * added in another. Per-file counts make that visible, and turn the baseline into a
 * worklist: `Phase 1` is "drive these files to zero", which can be sliced into
 * reviewable PRs by file.
 *
 * Usage:
 *   node scripts/check-i18n-strings.mjs            # gate
 *   node scripts/check-i18n-strings.mjs --update    # rewrite the baseline
 */

import { execFileSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const BASELINE = path.join(ROOT, 'src/i18n/untranslated-baseline.json')
const UPDATE = process.argv.includes('--update')

/**
 * Classification, so the baseline says what kind of work each file holds rather
 * than only how much. The categories map onto how a string gets fixed:
 * `expression` and `prose` are local edits, `template` and `object-prop` need the
 * value's shape to change first.
 */
function classify(text) {
  const t = text.replace(/^disallow literal string: /, '')
  if (/`/.test(t)) return 'template'
  if (/^\[/.test(t)) return 'array'
  if (/^(msg|message|error|title|label|description|placeholder|hint|summary|name|group|source):/.test(t)) return 'object-prop'
  if (/^(setError|setStatus|setMsg|notify|toast|alert|confirm)\(/.test(t)) return 'status-call'
  if (/^(aria-label|title|placeholder|alt)=/.test(t)) return 'attribute'
  if (/\?[^:]*:/.test(t)) return 'expression'
  return 'prose'
}

// Resolve eslint's own JS entry point and run it with `process.execPath`, NOT via `npx`.
// `execFileSync` does not go through a shell, and on Windows the installed shim is
// `npx.cmd` rather than `npx`, so `execFileSync('npx', …)` raises ENOENT — which lands
// in the catch below with an empty stdout and exits 2, aborting a REQUIRED gate on every
// Windows machine. Using `process.execPath` needs no shim and no `shell: true` (which
// would reintroduce quoting/injection concerns), and it pins the gate to the same Node
// that is already running this script.
const ESLINT_BIN = path.join(ROOT, 'node_modules', 'eslint', 'bin', 'eslint.js')

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
      '--config', 'eslint.i18n.config.js',
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

const report = JSON.parse(raw)
const byFile = {}
for (const file of report) {
  if (file.messages.length === 0) continue
  const rel = path.relative(path.join(ROOT, 'src'), file.filePath).split(path.sep).join('/')
  const counts = {}
  for (const m of file.messages) {
    const c = classify(m.message)
    counts[c] = (counts[c] || 0) + 1
  }
  byFile[rel] = { total: file.messages.length, ...Object.fromEntries(Object.entries(counts).sort()) }
}

const total = Object.values(byFile).reduce((a, f) => a + f.total, 0)
const current = {
  _comment:
    'Untranslated user-visible strings, per file. Generated — regenerate with '
    + '`node scripts/check-i18n-strings.mjs --update`. This is both the CI ratchet and '
    + 'the Phase 1 worklist: drive files to zero, one PR per file or per directory.',
  _total: total,
  files: Object.fromEntries(Object.entries(byFile).sort(([a], [b]) => a.localeCompare(b))),
}

if (UPDATE) {
  fs.writeFileSync(BASELINE, `${JSON.stringify(current, null, 2)}\n`)
  console.log(`wrote ${path.relative(ROOT, BASELINE)}: ${total} strings across ${Object.keys(byFile).length} files`)
  process.exit(0)
}

if (!fs.existsSync(BASELINE)) {
  console.error(`missing ${path.relative(ROOT, BASELINE)} — run with --update`)
  process.exit(2)
}
const base = JSON.parse(fs.readFileSync(BASELINE, 'utf-8'))

const grew = []
const shrank = []
const seen = new Set()
for (const [file, { total: now }] of Object.entries(byFile)) {
  seen.add(file)
  const then = base.files[file]?.total ?? 0
  if (now > then) grew.push(`  ${file}: ${then} → ${now}`)
  else if (now < then) shrank.push(`  ${file}: ${then} → ${now}`)
}
for (const [file, { total: then }] of Object.entries(base.files)) {
  if (!seen.has(file)) shrank.push(`  ${file}: ${then} → 0`)
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

if (shrank.length > 0) {
  console.error(
    `${shrank.length} file(s) improved — lock it in:\n${shrank.join('\n')}\n\n`
    + 'Run `node scripts/check-i18n-strings.mjs --update` and commit the baseline, so the\n'
    + 'gain cannot be given back. This is deliberate: a one-way ratchet never comes down.',
  )
  process.exit(1)
}

console.log(`OK: ${total} untranslated strings across ${Object.keys(byFile).length} files, at baseline.`)
