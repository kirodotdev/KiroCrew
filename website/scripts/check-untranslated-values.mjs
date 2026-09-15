#!/usr/bin/env node
/**
 * How many catalog values are still English where a translation is expected.
 *
 * ## Why this cannot fail the step
 *
 * The count is inherited. Eleven catalogs carry roughly 2,700 such values, and the
 * branch that happens to touch a catalog next did not put them there — failing on the
 * total would red-line PRs whose diff is clean, which is the exact failure mode the
 * gate runner's three-tier table exists to prevent. So this is an `info` row: it
 * measures, it prints, and it exits 0 with findings.
 *
 * Enforcement lives entirely in the diff: `check-source-strings.mjs` runs the same
 * predicates over the values a branch added or changed, at zero tolerance, where there
 * is no ceiling to raise and no one else to wait for.
 *
 * ## Why there is no committed ceiling either
 *
 * A stored total would only exist to notice growth, and the diff-scoped check already
 * refuses growth at the moment it is introduced. A second number would add a file to
 * update, a review ritual for raising it, and a way for two branches to conflict over
 * a count — buying nothing the zero-tolerance check does not already guarantee.
 *
 * Set `I18N_PASSTHROUGH_REPORT=1` for the per-key worklist; the default prints
 * per-locale counts, because 2,700 lines in every CI log is not a worklist anyone reads.
 *
 * Exit codes: 0 always, findings or not · 2 cannot run.
 */

import { readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { flatten } from './lib/qa-checks.mjs'
import {
  FUNCTION_WORDS,
  TARGET_SCRIPTS,
  passthroughChecks,
  passthroughViolations,
} from './lib/passthrough-checks.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const I18N = join(HERE, '..', 'src', 'i18n')
const LOCALES_DIR = join(I18N, 'locales')
const VERBOSE = process.env.I18N_PASSTHROUGH_REPORT === '1'

/** English is the source, and the pseudolocale is a mechanical transform of it. */
const SKIP = new Set(['en.json', 'en.manual.json', 'en-XA.json'])

const die = msg => {
  process.stdout.write(`\n[untranslated-passthrough] ${msg}\n`)
  process.exit(2)
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf-8'))
  } catch (err) {
    die(`could not read ${path}: ${err.message}`)
  }
}

const glossary = readJson(join(I18N, 'glossary.json'))
if (!Array.isArray(glossary.dnt) || !glossary.dnt.length) {
  die('glossary.json has no `dnt` array. Without it every product name reads as'
    + ' untranslated English, so the numbers below would be meaningless.')
}
const checks = passthroughChecks(glossary.dnt)

// The English source is split across two files with no overlapping keys; the
// identical-to-English half needs both or it silently skips a third of the corpus.
const enFlat = {
  ...flatten(readJson(join(LOCALES_DIR, 'en.manual.json'))),
  ...flatten(readJson(join(LOCALES_DIR, 'en.json'))),
}

const catalogs = readdirSync(LOCALES_DIR)
  .filter(f => f.endsWith('.json') && !SKIP.has(f))
  .sort()
if (!catalogs.length) die(`no locale catalog in ${LOCALES_DIR} — nothing to measure.`)

const findings = []
const perLocale = []
const unruled = []
let scanned = 0

for (const file of catalogs) {
  const lang = file.replace(/\.json$/, '')
  if (!TARGET_SCRIPTS[lang] && !FUNCTION_WORDS[lang]) unruled.push(lang)
  const catalog = flatten(readJson(join(LOCALES_DIR, file)))
  const total = Object.keys(catalog).length
  scanned += total
  const hits = passthroughViolations({ lang, catalog, enFlat, checks })
  findings.push(...hits)
  perLocale.push({ lang, total, hits })
}

process.stdout.write(`OK: ${findings.length} untranslated passthrough value(s) across `
  + `${catalogs.length} catalog(s) — ${scanned} value(s) scanned.\n`)

if (unruled.length) {
  process.stdout.write(`NOTE: no language rule for ${unruled.join(', ')} — `
    + 'these catalogs are counted as scanned but nothing about them is judged. Add the '
    + 'locale to TARGET_SCRIPTS or TARGET_FUNCTION in lib/passthrough-checks.mjs.\n')
}

const width = Math.max(...perLocale.map(p => p.lang.length))
for (const { lang, total, hits } of perLocale) {
  const byCheck = {}
  for (const h of hits) byCheck[h.check.id] = (byCheck[h.check.id] || 0) + 1
  const detail = Object.entries(byCheck).map(([id, n]) => `${id} ${n}`).join(' · ') || 'clean'
  const pct = total ? ((100 * hits.length) / total).toFixed(1) : '0.0'
  process.stdout.write(`  ${lang.padEnd(width)}  ${String(hits.length).padStart(5)} of ${total}`
    + `  (${pct}%)  ${detail}\n`)
}

if (findings.length && !VERBOSE) {
  process.stdout.write('\nSet I18N_PASSTHROUGH_REPORT=1 for the per-key worklist.\n')
}

if (findings.length && VERBOSE) {
  process.stdout.write('\n')
  for (const { lang, hits } of perLocale) {
    if (!hits.length) continue
    process.stdout.write(`${lang}\n`)
    for (const h of hits) {
      process.stdout.write(`  ${h.check.id}  ${h.key}\n    ${JSON.stringify(h.value)}\n`)
    }
    process.stdout.write('\n')
  }
}

process.exit(0)
