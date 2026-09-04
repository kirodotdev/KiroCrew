#!/usr/bin/env node
/**
 * DNT integrity, asserted against catalog VALUES rather than rendered pixels.
 *
 * ## Why this is a separate script and not part of the render gate
 *
 * DNT (do-not-translate) protects proper nouns — `AWS`, `GitHub`, `Playwright` — from
 * a translator who transliterates or "helpfully" localises them. It used to be
 * checked by `check-i18n-render.mjs`, which meant it could only ever run in a locale
 * the render budget could afford. It cannot run under `en-XA` at all:
 * `gen-pseudolocale.mjs` accents every ASCII letter outside a preserved region and
 * DNT terms are not preserved, so all of them render mangled by design.
 *
 * That coupling made a zero-tolerance assertion hostage to a render budget. When the
 * locale list came down to `en-XA` alone (55+ surfaces x 2 trees does not fit the
 * `e2e` job's 25 minutes with real locales in it), the render gate had no locale left
 * to check DNT in — and a gate that renders, reports and exits 0 while checking no
 * DNT term is worse than no gate, because it looks green.
 *
 * Comparing catalog values instead is strictly better on every axis that matters:
 * it covers ALL shipped locales rather than one probe, it needs no browser, no build
 * and no fixtures, and it runs in milliseconds.
 *
 * ## What it asserts
 *
 * Exactly what the render-time check asserted, via the SAME function —
 * `dntViolations` from `lib/render-scan.mjs`, applied to catalog values instead of
 * rendered text. Sharing the implementation is the point: a second, subtly different
 * DNT rule is how two gates start disagreeing about what a violation is.
 *
 * That function is a NEAR-MISS detector, not a presence check. It matches each term
 * loosely (case-insensitively, with every separator optional) and then requires the
 * hit to be byte-exact, so it reports `Github`, `NodeJS`, `Node JS`, `YAml` — wrong
 * internal capitalisation or spacing, which is the class DNT protects against. Two
 * things it deliberately does NOT report, and both matter here:
 *
 *   - **An all-lowercase hit** is the command or package, not the product: `git push`,
 *     `npm i`, `python -m`. Prose about denied commands is full of them.
 *   - **A term that is absent.** German genitive inflects (`Kiros eigenem Standard`),
 *     and a language that restructures the sentence may drop the addressee entirely
 *     (`Tell Kiro about you` -> `介绍一下你自己`). Both are correct translations, and a
 *     presence check flags both — I wrote one first and it failed on main's own
 *     catalogs for exactly these two strings.
 *
 * `en-XA` is skipped: it is generated, and accenting ASCII is its whole job.
 * Untranslated keys are skipped too — `check-i18n-keys.mjs` owns key coverage, and
 * reporting a gap twice makes two gates fail for one cause.
 *
 * Exit codes: 0 clean · 1 violations found · 2 cannot run.
 */

import { readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import { dntViolations } from './lib/render-scan.mjs'

const HERE = dirname(fileURLToPath(import.meta.url))
const I18N = join(HERE, '..', 'src', 'i18n')
const LOCALES_DIR = join(I18N, 'locales')

/** Catalogs that are generated or are themselves the source — never checked. */
const SKIP = new Set(['en.json', 'en.manual.json', 'en-XA.json'])

const die = msg => {
  process.stdout.write(`\n[dnt] ${msg}\n`)
  process.exit(2)
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, 'utf-8'))
  } catch (err) {
    die(`could not read ${path}: ${err.message}`)
  }
}

/**
 * Flatten a nested catalog to `dotted.key -> string`.
 *
 * Catalogs are nested objects; comparing them key-for-key needs one shape. Arrays
 * are flattened by index so a term inside a list value is still covered.
 */
function flatten(node, prefix = '', out = {}) {
  if (typeof node === 'string') {
    out[prefix] = node
    return out
  }
  if (Array.isArray(node)) {
    node.forEach((v, i) => flatten(v, prefix ? `${prefix}.${i}` : String(i), out))
    return out
  }
  if (node && typeof node === 'object') {
    for (const [k, v] of Object.entries(node)) {
      flatten(v, prefix ? `${prefix}.${k}` : k, out)
    }
  }
  return out
}

const glossary = readJson(join(I18N, 'glossary.json'))
if (!Array.isArray(glossary.dnt) || !glossary.dnt.length) {
  die('glossary.json has no `dnt` array — nothing to enforce, which is never correct.'
    + ' Restore it rather than letting this gate pass vacuously.')
}
const terms = glossary.dnt

const catalogs = readdirSync(LOCALES_DIR)
  .filter(f => f.endsWith('.json') && !SKIP.has(f))
  .sort()
if (!catalogs.length) {
  die(`no locale catalog to check in ${LOCALES_DIR} — this gate would pass vacuously.`)
}

const findings = []
let values = 0

for (const file of catalogs) {
  const flat = flatten(readJson(join(LOCALES_DIR, file)))
  for (const [key, value] of Object.entries(flat)) {
    if (typeof value !== 'string' || !value) continue
    values += 1
    for (const { term, found } of dntViolations(value, terms)) {
      findings.push({ file, key, term, found, value })
    }
  }
}

if (findings.length) {
  process.stdout.write(`\n[dnt] ${findings.length} do-not-translate violation(s) across `
    + `${new Set(findings.map(f => f.file)).size} catalog(s):\n\n`)
  for (const f of findings) {
    process.stdout.write(`  ${f.file}  ${f.key}\n`)
    process.stdout.write(`    "${f.found}" should be "${f.term}"\n`)
    process.stdout.write(`    ${JSON.stringify(f.value)}\n\n`)
  }
  process.stdout.write('A DNT term names a product, a company or a tool, so respelling it makes\n'
    + 'the string wrong rather than localised. Fix the translation; do not add the term\n'
    + 'to an allowlist. An all-lowercase form is already exempt as the command name.\n')
  process.exit(1)
}

process.stdout.write(`OK: ${terms.length} DNT term(s) intact across ${catalogs.length} `
  + `catalog(s) — ${values} value(s) scanned.\n`)
