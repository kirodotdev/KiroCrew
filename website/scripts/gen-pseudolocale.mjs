#!/usr/bin/env node
/**
 * Generate the `en-XA` pseudolocale from the English catalog.
 *
 * Pseudolocalization is the only detector in this plan that needs no heuristic. In an
 * `en-XA` build, "is this string translated?" has a mechanical answer: translated text
 * is accented and bracketed, untranslated text is plain Latin. Three successive static
 * scanners failed to report the same two untranslated buttons on the Portability tab
 * that a person spotted at a glance in a screenshot; this catches that class by
 * construction rather than by pattern.
 *
 * It also catches two things no catalog assertion can: **text expansion**, because the
 * padding simulates the 30–200 % that real translation adds, and **sentence
 * concatenation**, because each string is wrapped in brackets — so a sentence split
 * across two keys renders as `[…] [：…]` and the seam is visible.
 *
 * `en-XA` is the ICU/Android private-use convention for an accented pseudolocale.
 * Windows once used a real locale tag (`tk-TM`) for this and broke its own CI, which is
 * why the tag has to come from the private-use range.
 *
 * ## Five things this must get right, each of which silently breaks it
 *
 * 1. **Source is `en.json` ⊕ `en.manual.json`.** The 89 hand-authored keys include the
 *    whole left nav. Generating from `en.json` alone leaves them plain English inside
 *    the one detector promised to have no false positives.
 * 2. **Placeholders are never transformed.** Accenting `{{count}}` breaks interpolation
 *    at runtime, and `catalogParity`'s placeholder assertion would fail on the generated
 *    file. Verified by asserting placeholder-set equality per key.
 * 3. **Markup and URL-shaped values are never transformed.** The catalog carries a few
 *    `<…>` values that are URL placeholders, not markup. The preserved regions must be
 *    matched in ONE pass over the original value: masking them one pattern at a time let
 *    the URL pattern swallow an already-masked `<owner>` token, and the restore pass left
 *    the inner sentinel behind as a literal NUL character. See `carvePreserved`.
 * 4. **Plural forms come from `pluralKeys.json`, never from a suffix regex.** 47 keys end
 *    in the English word "one" or "other" as real copy (`click_new_to_create_one`).
 *    `Intl.PluralRules('en-XA')` resolves to `en`, so the category set is unchanged and
 *    the keys pass through untouched structurally.
 * 5. **Expansion is proportional and capped.** The IBM table via W3C: a string of ≤10
 *    characters can grow 200–300 %, one over 70 characters only 130 %. A flat +40 %
 *    under-tests short labels, which are the highest-risk surface, so the ratio is a
 *    function of length.
 *
 * Usage:
 *   node scripts/gen-pseudolocale.mjs           # write the catalog
 *   node scripts/gen-pseudolocale.mjs --check    # fail if it is stale
 */

import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const LOCALES = path.resolve(__dirname, '../src/i18n/locales')
const OUT = path.join(LOCALES, 'en-XA.json')
const CHECK = process.argv.includes('--check')

/**
 * Latin → accented Latin. Deliberately preserves the base letter's silhouette so the
 * text stays readable to a reviewer who has to judge layout, while being unmistakably
 * not-English at a glance.
 */
const ACCENTS = {
  a: 'à', b: 'ƀ', c: 'ç', d: 'ð', e: 'è', f: 'ƒ', g: 'ğ', h: 'ĥ', i: 'ì', j: 'ĵ',
  k: 'ķ', l: 'ĺ', m: 'ɱ', n: 'ñ', o: 'ø', p: 'þ', q: 'ǫ', r: 'ŕ', s: 'ş', t: 'ţ',
  u: 'ù', v: 'ṽ', w: 'ẁ', x: 'ẋ', y: 'ý', z: 'ž',
  A: 'À', B: 'Ɓ', C: 'Ç', D: 'Ð', E: 'È', F: 'Ƒ', G: 'Ğ', H: 'Ĥ', I: 'Ì', J: 'Ĵ',
  K: 'Ķ', L: 'Ĺ', M: 'Ṁ', N: 'Ñ', O: 'Ø', P: 'Þ', Q: 'Ǫ', R: 'Ŕ', S: 'Ş', T: 'Ţ',
  U: 'Ù', V: 'Ṽ', W: 'Ẁ', X: 'Ẋ', Y: 'Ý', Z: 'Ž',
}

/** Padding that reads as filler rather than as words, so it cannot be mistaken for copy. */
const PAD = '·'

/**
 * IBM/W3C expansion budget. The shorter the source, the larger the proportional growth,
 * because a one-word button is the case most likely to overflow.
 */
function expansionRatio(length) {
  if (length <= 10) return 2.5
  if (length <= 20) return 1.9
  if (length <= 30) return 1.7
  if (length <= 50) return 1.5
  if (length <= 70) return 1.35
  return 1.3
}

/** Regions of a value that must pass through byte-identical. */
const PRESERVE = [
  /\{\{[^}]*\}\}/g, // i18next interpolation
  /<[^>]+>/g, // markup, and the URL-shaped `<…>` values in this catalog
  /\$t\([^)]*\)/g, // i18next nesting
  /https?:\/\/\S+/g, // urls
]

/** The masking sentinel, as a non-global regex so `.test()` carries no `lastIndex`. */
const SENTINEL = /\u0000\d+\u0000/
const SENTINEL_G = /\u0000(\d+)\u0000/g

/**
 * Carve out the `PRESERVE` regions in ONE pass, outermost region wins.
 *
 * Applying the patterns SEQUENTIALLY corrupted the catalog. In
 * `https://github.com/<owner>/<repo>` the markup pattern masked `<owner>` and `<repo>`
 * first, and the URL pattern then swallowed the whole already-masked span — `\S+`
 * matches the NUL sentinel — so one hole ended up nested inside another. The restore
 * pass substitutes each sentinel exactly once, so the inner two were never restored and
 * four committed keys carried literal NUL characters instead of their markup.
 *
 * Matching every pattern against the ORIGINAL value and keeping the enclosing region
 * makes nesting unrepresentable: the whole URL becomes a single hole, which is what
 * "preserve URL-shaped values byte-identical" meant in the first place. A region that
 * overlaps a kept one without being contained by it EXTENDS that region rather than
 * being dropped, so this can only ever preserve more text than the old code, never less.
 */
function carvePreserved(value) {
  const ranges = []
  for (const re of PRESERVE) {
    for (const m of value.matchAll(re)) ranges.push([m.index, m.index + m[0].length])
  }
  // Start ascending, then longest first, so an enclosing region is always visited
  // before anything nested inside it.
  ranges.sort((a, b) => a[0] - b[0] || b[1] - a[1])

  const kept = []
  for (const [start, end] of ranges) {
    const last = kept[kept.length - 1]
    if (last !== undefined && start < last[1]) {
      // Fully nested (end <= last[1]) is already covered; a partial overlap widens the
      // kept region so no preserved character is left outside a hole.
      if (end > last[1]) last[1] = end
      continue
    }
    kept.push([start, end])
  }

  const holes = []
  let masked = ''
  let cursor = 0
  for (const [start, end] of kept) {
    masked += `${value.slice(cursor, start)}\u0000${holes.length}\u0000`
    holes.push(value.slice(start, end))
    cursor = end
  }
  masked += value.slice(cursor)
  return { masked, holes }
}

function transform(value) {
  // Carve out every region that must survive untouched, transform what is left, then
  // splice the carved regions back in at their original positions.
  const { masked, holes } = carvePreserved(value)

  const accented = masked.replace(/[A-Za-z]/g, (c) => ACCENTS[c] ?? c)

  // Pad to the length budget, measured on the visible text only so a value that is
  // mostly placeholder does not get padded on their behalf.
  const visible = accented.replace(/\u0000\d+\u0000/g, '').length
  const target = Math.round(visible * expansionRatio(visible))
  const padding = Math.max(0, target - visible)
  const padded = `${accented}${padding > 0 ? ` ${PAD.repeat(padding)}` : ''}`

  // Brackets delimit the string, which is what makes a split sentence visible as
  // `[…] […]` rather than reading as one phrase.
  const bracketed = `[${padded}]`

  // A single pass is now sufficient, because `carvePreserved` cannot emit a nested hole.
  // The loop is defence in depth against a future `PRESERVE` entry that reintroduces
  // nesting, and is bounded by the hole count so a malformed sentinel cannot spin.
  let restored = bracketed
  for (let pass = 0; pass <= holes.length && SENTINEL.test(restored); pass += 1) {
    restored = restored.replace(SENTINEL_G, (_, i) => holes[Number(i)])
  }
  return restored
}

function mapLeaves(obj, fn) {
  if (obj === null || typeof obj !== 'object') return fn(String(obj))
  const out = {}
  for (const [k, v] of Object.entries(obj)) out[k] = mapLeaves(v, fn)
  return out
}

function merge(a, b) {
  const out = { ...a }
  for (const [k, v] of Object.entries(b)) {
    const cur = out[k]
    out[k] = v !== null && typeof v === 'object' && !Array.isArray(v)
      && cur !== null && typeof cur === 'object' && !Array.isArray(cur)
      ? merge(cur, v)
      : v
  }
  return out
}

function flatten(obj, prefix = '') {
  const out = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [k, v] of Object.entries(obj)) {
    const p = prefix ? `${prefix}.${k}` : k
    if (v !== null && typeof v === 'object') Object.assign(out, flatten(v, p))
    else out[p] = String(v)
  }
  return out
}

const read = (f) => JSON.parse(fs.readFileSync(path.join(LOCALES, f), 'utf-8'))
// Source is the MERGED English catalog — see note 1 in the header.
const source = merge(read('en.json'), read('en.manual.json'))
const generated = mapLeaves(source, transform)
const serialized = `${JSON.stringify(generated, null, 2)}\n`

// Self-check: every key's placeholder multiset must survive the transform. A pseudolocale
// that breaks interpolation is a defect generator, not a detector.
const flatSource = flatten(source)
const flatOut = flatten(generated)
const placeholders = (v) => (v.match(/\{\{[^}]*\}\}/g) ?? []).sort().join('|')
const broken = Object.keys(flatSource).filter(
  (k) => placeholders(flatSource[k]) !== placeholders(flatOut[k]),
)
if (broken.length > 0) {
  console.error(
    `${broken.length} key(s) lost or gained a placeholder during generation — the `
    + `transform is wrong, do not commit:\n${broken.slice(0, 10).map((k) => `  ${k}`).join('\n')}`,
  )
  process.exit(2)
}
if (Object.keys(flatSource).length !== Object.keys(flatOut).length) {
  console.error('key count changed during generation — the transform is wrong')
  process.exit(2)
}

// Invariant: no masking sentinel may survive into the catalog. The sequential-masking
// bug wrote four keys containing literal NUL characters instead of their `<owner>` /
// `<repo>` markup and nothing in the pipeline noticed — placeholder parity passes
// happily on a value whose markup has been replaced by control characters. Assert the
// absence directly, so this whole class of defect fails generation instead of being
// caught by a reviewer reading a 4636-line blob.
const corrupt = Object.keys(flatOut).filter((k) => flatOut[k].includes('\u0000'))
if (corrupt.length > 0) {
  console.error(
    `${corrupt.length} key(s) still contain a masking sentinel (U+0000) — a preserved `
    + `region was carved out and never restored, do not commit:\n${
      corrupt.slice(0, 10).map((k) => `  ${k}`).join('\n')}`,
  )
  process.exit(2)
}

if (CHECK) {
  const existing = fs.existsSync(OUT) ? fs.readFileSync(OUT, 'utf-8') : ''
  if (existing !== serialized) {
    console.error(
      'src/i18n/locales/en-XA.json is stale.\n'
      + 'Run `node scripts/gen-pseudolocale.mjs` and commit the result — the pseudolocale '
      + 'has to track en.json or it stops detecting anything.',
    )
    process.exit(1)
  }
  console.log(`OK: en-XA.json matches ${Object.keys(flatSource).length} English keys.`)
  process.exit(0)
}

fs.writeFileSync(OUT, serialized)
console.log(`wrote src/i18n/locales/en-XA.json (${Object.keys(flatSource).length} keys)`)
