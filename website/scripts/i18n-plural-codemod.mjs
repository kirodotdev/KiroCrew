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
 *   node scripts/i18n-plural-codemod.mjs [--check]
 */

import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const SRC = path.join(ROOT, 'src')
const CHECK = process.argv.includes('--check')

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

for (const file of walk(SRC)) {
  const before = fs.readFileSync(file, 'utf-8')
  let after = before

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
  if (changedFiles.length) {
    console.error(
      `\nFAIL: ${changedFiles.length} file(s) still use the literal-'s' plural hack:\n`
      + changedFiles.map(f => `  ${f}`).join('\n')
      + `\n\nUse i18next plurals instead: i18nT('key', { count: n }) with _one/_other`
      + ` catalog forms. Appending 's' outside i18nT() cannot be fixed by any`
      + ` translation — see scripts/i18n-plural-codemod.mjs.\n`,
    )
    process.exit(1)
  }
  console.log('OK: no literal-\'s\' pluralization found.')
} else {
  console.log(`rewrote ${changedFiles.length} file(s), ${touchedKeys.size} key(s)`)
  console.log([...touchedKeys].sort().join('\n'))
}
