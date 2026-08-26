#!/usr/bin/env node
/**
 * Replace the `+ 's'` pluralization hack with i18next's native plural API.
 *
 * ## The defect this removes
 *
 * 33 call sites rendered a count by gluing a literal English `s` onto a
 * TRANSLATED noun:
 *
 *     {n} {i18nT('pages.overview.memoryTab.session')}{n === 1 ? '' : 's'}
 *
 * In English that yields "3 sessions". In every other language it yields a
 * non-word: zh-CN已 shipped `会话s`, and the six new catalogs produced
 * `3 sesións`, `2 sitio estáticos`, `এজেন্টs`, `Excluir 3 job agendados?`.
 * The `s` is appended OUTSIDE `i18nT()`, so no catalog value can fix it —
 * only removing the concatenation can.
 *
 * ## The fix
 *
 * Hand the count to i18next and let it select the form:
 *
 *     {i18nT('pages.overview.memoryTab.session', { count: n })}
 *
 * with `_one` / `_other` (and `_few` / `_many` where a language needs them)
 * keys in the catalogs. i18next resolves the plural category through
 * `Intl.PluralRules`, so each language gets its OWN rules rather than English's:
 * Russian selects between 4 forms, Spanish/French/Portuguese 3, Hindi/Bengali 2,
 * and Chinese 1. That is not expressible as string concatenation at the call
 * site, which is why this had to move into the catalog.
 *
 * The count moves INSIDE the translated string (`{{count}} sessions`) rather
 * than staying a separate JSX expression, because word order is language-
 * specific — a translation must be free to place the number where its grammar
 * requires.
 *
 * ## Why a codemod rather than 33 hand edits
 *
 * Same reason as `i18n-codemod.mjs`: one auditable tool beats 33 near-identical
 * diffs, and it can be re-run if an upstream sync reintroduces the pattern.
 * Run with `--check` in CI to fail on reintroduction.
 *
 * ## The `--check` contract has two tiers
 *
 * 1. **i18nT-adjacent glue — HARD ZERO.** The patterns below, where the plural
 *    ternary follows an `i18nT('key')` call. All 33 were converted, so any
 *    match is a reintroduction and fails outright.
 * 2. **Fully hardcoded literals — CEILING, fails only on growth.** The same
 *    defect with no `i18nT` anywhere in it (`scripts/lib/hardcoded-plural.mjs`
 *    is the detector). Those sites cannot be auto-converted — each needs a new
 *    catalog key plus translations — so the frozen debt is pinned at
 *    `HARDCODED_CEILING` and only a NEW site fails the check. The ceiling
 *    ratchets DOWN as sites are converted; see the constant's comment for the
 *    one sanctioned reason to raise it.
 *
 *   node scripts/i18n-plural-codemod.mjs [--check]
 */

import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'
import { findHardcodedPluralSites } from './lib/hardcoded-plural.mjs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const SRC = path.join(ROOT, 'src')
const CHECK = process.argv.includes('--check')

/**
 * Ceiling for the SECOND tier (see the header): fully hardcoded plural
 * literals, which the i18nT-anchored patterns below cannot see. `--check`
 * fails only when the count GROWS past this number; lower it as sites are
 * converted. Raising it is reserved for exactly one case: a concurrent merge
 * added a site, so an unrelated PR inherits the red — that PR may raise the
 * ceiling with a line explaining the growth is inherited, or convert the added
 * site. A measured count, not a target: 7 template-literal sites remain after
 * the current main-branch conversions, plus the 3 whole-word-ternary sites
 * this detector widening makes visible (PastedChip.tsx, SecurityPanel.tsx,
 * AgentImportFlow.tsx). The other two widened spellings (JSX-text glue,
 * string concatenation) have zero live sites, so a new site in them fails the
 * check for as long as the aggregate count sits at the ceiling — the ceiling
 * is one number over all spellings, so unclaimed slack from a converted site
 * in one spelling can absorb a new site in another until the constant is
 * tightened.
 */
const HARDCODED_CEILING = 10

/**
 * The two shapes in the codebase, both anchored so the count expression is
 * captured and matched against the SAME expression used in the ternary — a
 * mismatch means the `s` is driven by a different variable than the number
 * being displayed, which is a bug this codemod must not silently "fix".
 *
 *   {expr} {i18nT('key')}{expr === 1 ? '' : 's'}
 *   {expr} {i18nT('key')}{expr !== 1 ? 's' : ''}
 */
const PATTERNS = [
  // eslint-disable-next-line no-useless-escape
  /\{([^{}]+?)\}(\s*)\{i18nT\('([^']+)'\)\}\{([^{}]+?) === 1 \? '' : 's'\}/g,
  /\{([^{}]+?)\}(\s*)\{i18nT\('([^']+)'\)\}\{([^{}]+?) !== 1 \? 's' : ''\}/g,
  // `> 1 ? 's' : ''` — same defect, different spelling. Worth matching
  // explicitly: an upstream sync reintroduced exactly this shape, and a guard
  // that only knows `=== 1`/`!== 1` would report the file as clean.
  /\{([^{}]+?)\}(\s*)\{i18nT\('([^']+)'\)\}\{([^{}]+?) > 1 \? 's' : ''\}/g,
]

/** Sites where the noun follows a count that is NOT its immediate sibling. */
const STANDALONE = [
  /\{i18nT\('([^']+)'\)\}\{([^{}]+?) === 1 \? '' : 's'\}/g,
  /\{i18nT\('([^']+)'\)\}\{([^{}]+?) !== 1 \? 's' : ''\}/g,
  /\{i18nT\('([^']+)'\)\}\{([^{}]+?) > 1 \? 's' : ''\}/g,
]

function walk(dir, out = []) {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name)
    if (e.isDirectory()) {
      if (e.name === 'node_modules' || e.name === 'dist') continue
      walk(p, out)
    } else if (/\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name)) {
      out.push(p)
    }
  }
  return out
}

const touchedKeys = new Set()
const changedFiles = []
const skipped = []
const hardcodedSites = []

for (const file of walk(SRC)) {
  const before = fs.readFileSync(file, 'utf-8')
  let after = before

  // Tier two, counted but never rewritten: fully hardcoded plural glue. Each
  // site needs a NEW catalog key plus translations, which no codemod can
  // invent — so it is only counted against HARDCODED_CEILING.
  for (const site of findHardcodedPluralSites(before)) {
    hardcodedSites.push({ file: path.relative(ROOT, file), line: site.line, text: site.text })
  }

  for (const re of PATTERNS) {
    after = after.replace(re, (m, countExpr, gap, key, ternaryExpr) => {
      // The displayed count and the pluralizing count MUST be the same
      // expression, or the rendered number and the chosen form disagree.
      if (countExpr.trim() !== ternaryExpr.trim()) {
        skipped.push(`${path.relative(ROOT, file)}: count '${countExpr.trim()}' != ternary '${ternaryExpr.trim()}' for ${key}`)
        return m
      }
      touchedKeys.add(key)
      return `{i18nT('${key}', { count: ${countExpr.trim()} })}`
    })
  }

  // Anything still matching is a standalone form the paired pattern missed.
  for (const re of STANDALONE) {
    after = after.replace(re, (m, key, ternaryExpr) => {
      touchedKeys.add(key)
      return `{i18nT('${key}', { count: ${ternaryExpr.trim()} })}`
    })
  }

  if (after !== before) {
    changedFiles.push(path.relative(ROOT, file))
    if (!CHECK) fs.writeFileSync(file, after)
  }
}

for (const s of skipped) console.error(`SKIPPED (needs a human): ${s}`)

/**
 * Registry of pluralized base keys, consumed by `catalogParity.test.ts`.
 *
 * Written here rather than derived from a `_one`/`_other` suffix scan, because
 * real copy ends in those words ("panel to add one.", "Click + New to create
 * one."). Only this codemod pluralizes a key, so only it can say which keys are
 * plural — that makes the registry incapable of drifting from the call sites.
 */
const REGISTRY = path.join(ROOT, 'src/i18n/pluralKeys.json')

if (!CHECK && touchedKeys.size) {
  const existing = fs.existsSync(REGISTRY)
    ? JSON.parse(fs.readFileSync(REGISTRY, 'utf-8'))
    : []
  const merged = [...new Set([...existing, ...touchedKeys])].sort()
  fs.writeFileSync(REGISTRY, JSON.stringify(merged, null, 2) + '\n')
  console.log(`registry: ${path.relative(ROOT, REGISTRY)} (${merged.length} keys)`)
}

if (CHECK) {
  // Both tiers are evaluated and printed before the exit, for the same reason
  // `i18n-check.mjs` replaced the `&&` chain: short-circuiting on the first
  // failure means an author learns about the second one a CI round later.
  let failed = false

  if (changedFiles.length) {
    failed = true
    console.error(
      `\nFAIL: ${changedFiles.length} file(s) still use the literal-'s' plural hack:\n`
      + changedFiles.map(f => `  ${f}`).join('\n')
      + `\n\nUse i18next plurals instead: i18nT('key', { count: n }) with _one/_other`
      + ` catalog forms. Appending 's' outside i18nT() cannot be fixed by any`
      + ` translation — see scripts/i18n-plural-codemod.mjs.\n`,
    )
  } else {
    console.log('OK: no literal-\'s\' pluralization found.')
  }

  const n = hardcodedSites.length
  if (n > HARDCODED_CEILING) {
    failed = true
    console.error(
      `\nFAIL: ${n} hardcoded plural literal(s) — ${n - HARDCODED_CEILING} above the ceiling of ${HARDCODED_CEILING}:\n`
      + hardcodedSites.map(s => `  ${s.file}:${s.line}  ${s.text}`).join('\n')
      + `\n\nEvery site is listed so yours is findable: the one(s) this branch added are`
      + `\nthe growth. Replace the glued suffix with i18nT('key', { count: n }) and`
      + `\n_one/_other catalog forms — see this script's header for why no hardcoded`
      + `\nspelling can be correct in 12 languages. If no listed site is yours, a`
      + `\nconcurrent merge grew the count under you: convert the added site, or raise`
      + `\nHARDCODED_CEILING in the same PR with a line explaining the growth is`
      + `\ninherited.\n`,
    )
  } else if (n < HARDCODED_CEILING) {
    console.log(
      `OK: ${n} hardcoded plural literal(s), below the ceiling of ${HARDCODED_CEILING}. `
      + `Optional: lower HARDCODED_CEILING to ${n} in scripts/i18n-plural-codemod.mjs to tighten the ratchet.`,
    )
  } else {
    console.log(`OK: ${n} hardcoded plural literal(s), at the ceiling of ${HARDCODED_CEILING}.`)
  }

  // exitCode rather than process.exit(): an immediate exit can terminate the
  // process before piped stdout/stderr flush, and the runner reads a silent
  // child as MISSING rows — a truncated verdict, not a fast one.
  process.exitCode = failed ? 1 : 0
} else {
  console.log(`rewrote ${changedFiles.length} file(s), ${touchedKeys.size} key(s)`)
  console.log([...touchedKeys].sort().join('\n'))
  if (hardcodedSites.length) {
    console.log(
      `\n${hardcodedSites.length} hardcoded plural literal(s) need MANUAL conversion `
      + `(each needs a new catalog key, so no codemod can rewrite them):\n`
      + hardcodedSites.map(s => `  ${s.file}:${s.line}  ${s.text}`).join('\n'),
    )
  }
}
