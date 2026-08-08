#!/usr/bin/env node
/**
 * Translation driver: renders the committed prompt per (locale, shard), validates
 * filled shards, and drives `i18n-shard.mjs join` across every shipped locale.
 *
 * `i18n-shard.mjs` is deliberately single-locale — `join <dir> <tag>` writes one
 * catalog per invocation. That is the right shape for the primitive and the wrong
 * shape for the job: Phase 1 adds ~1767 keys, and `catalogParity.test.ts` demands
 * every one of them in all 11 non-English catalogs in the SAME commit, so a
 * translation run is inherently an 11-way fan-out. Doing that by hand is eleven
 * chances to skip a locale and discover it as a red `Frontend Tests`.
 *
 * This script does NOT call a model. It renders the prompt and checks the answer;
 * sending it is the caller's job (an agent, a contributor, whatever). That split is
 * deliberate — it keeps the pipeline reproducible and testable with no network, no
 * credentials, and no vendor pinned into the repo.
 *
 * Usage:
 *   node scripts/i18n-translate.mjs plan [pathPrefix]        # what needs translating
 *   node scripts/i18n-translate.mjs emit <baseDir> [--locales a,b]
 *   node scripts/i18n-translate.mjs verify <baseDir> --locale <tag>
 *   node scripts/i18n-translate.mjs merge <baseDir> [--overwrite]
 *
 * `merge`, never `i18n-shard.mjs join`, for a translation produced here. `join`
 * rewrites the catalog from shards keyed off the ENGLISH corpus, so any form the
 * locale has and English does not is silently dropped — a measured round-trip
 * removes 108 lines from `ru.json` and 45 keys from each of es/fr/pt/it, all
 * `_few`/`_many` CLDR plural forms. It also cannot accept the locale-specific
 * plural keys `emit` now asks for, since it validates against the English key set.
 * `merge` is insert-only by default and preserves both.
 *
 * Layout under <baseDir> (keep it OUTSIDE the worktree — a dirty tree blocks
 * worktree pruning, see website/AGENTS.md Rule 9):
 *   en/       shard-NN.json + shard-NN.context.json   <- `i18n-shard.mjs split` output
 *   prompts/  <locale>/shard-NN.prompt.md             <- `emit` writes these
 *   <locale>/ shard-NN.json                           <- filled translations
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const SRC = path.join(ROOT, 'src/i18n')

const PROMPT_FILE = path.join(SRC, 'TRANSLATION-PROMPT.md')
const GLOSSARY_FILE = path.join(SRC, 'glossary.json')
const CONTEXT_FILE = path.join(SRC, 'en.context.json')
const STYLE_DIR = path.join(SRC, 'style')
const LANGUAGES_FILE = path.join(SRC, 'languages.ts')
const BASELINE_FILE = path.join(SRC, 'untranslated-baseline.json')
const PLURAL_KEYS_FILE = path.join(SRC, 'pluralKeys.json')
const LOCALES_DIR = path.join(SRC, 'locales')

/**
 * Categories `check-i18n-strings.mjs` assigns that Phase 1 owns. The other three
 * — `template`, `object-prop`, `array` — need a change of shape rather than a
 * local edit, so they belong to Phase 6 and must not inflate a Phase 1 estimate.
 */
export const PHASE1_CATEGORIES = ['expression', 'prose', 'attribute', 'status-call']
export const PHASE6_CATEGORIES = ['template', 'object-prop', 'array']

/**
 * Parse the shipped locales out of `languages.ts` rather than duplicating them.
 * A second hardcoded list is a second thing to forget when language #13 ships;
 * `translateDriver.test.ts` asserts this parse against the real
 * `SUPPORTED_LANGUAGES` so a format change here fails loudly instead of silently
 * translating ten languages out of eleven.
 */
export function parseLanguages(source) {
  const block = source.match(/SUPPORTED_LANGUAGES[^=]*=\s*\[([\s\S]*?)\n\]/)
  if (!block) throw new Error('could not locate SUPPORTED_LANGUAGES in languages.ts')
  const out = []
  for (const m of block[1].matchAll(/\{\s*code:\s*'([^']+)'\s*,\s*label:\s*'([^']+)'([^}]*)\}/g)) {
    if (/devOnly:\s*true/.test(m[3])) continue
    out.push({ code: m[1], label: m[2] })
  }
  if (out.length === 0) throw new Error('parsed zero languages from languages.ts')
  return out
}

const ALL_LANGUAGES = parseLanguages(fs.readFileSync(LANGUAGES_FILE, 'utf-8'))
const DEFAULT_LANGUAGE = 'en'
const TARGET_LANGUAGES = ALL_LANGUAGES.filter(l => l.code !== DEFAULT_LANGUAGE)

// ---------------------------------------------------------------------------
// pure helpers (exported for the test suite)
// ---------------------------------------------------------------------------

export function flatten(obj, prefix = '') {
  const out = {}
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object' && !Array.isArray(v)) Object.assign(out, flatten(v, key))
    else out[key] = v
  }
  return out
}

/** i18next interpolation, tags, and `$t()` nesting — the things that are code. */
export function placeholders(value) {
  return [
    ...String(value).matchAll(/\{\{[^}]*\}\}/g),
    ...String(value).matchAll(/<\/?[^>]+>/g),
    ...String(value).matchAll(/\$t\([^)]*\)/g),
  ].map(m => m[0]).sort()
}

/**
 * Every rule `verify` can decide mechanically. Ordered so the cheapest and most
 * certain failures report first.
 *
 * `dnt` is word-boundary, matching `glossary.test.ts` — a substring check would
 * flag any word that merely contains `Git`, and a term absent from the English
 * is not required to appear in the translation.
 */
export function checkValue({
  key, en, tr, dnt = [], categories = null, pluralBases = null, locale = null,
}) {
  const findings = []
  const push = (rule, detail) => findings.push({ key, rule, detail })

  if (typeof tr !== 'string') return [{ key, rule: 'not-a-string', detail: typeof tr }]
  if (tr.trim() === '') return [{ key, rule: 'empty', detail: 'value is empty or whitespace' }]

  const ph = { en: placeholders(en), tr: placeholders(tr) }
  if (ph.en.join('|') !== ph.tr.join('|')) {
    push('placeholder-parity', `en=[${ph.en.join(' ')}] tr=[${ph.tr.join(' ')}]`)
  }

  const nl = s => (String(s).match(/\n/g) ?? []).length
  if (nl(en) !== nl(tr)) push('newline-count', `en=${nl(en)} tr=${nl(tr)}`)

  // Hangul immediately after the term is a BOUNDARY, not a continuation of it.
  // Korean agglutinates — a 조사 attaches straight onto the Latin run
  // (`GitHub에서`, `MCP를`) — so a plain letter-boundary reports every correct
  // Korean use of a do-not-translate term as a dropped one: 54 findings on a
  // catalog that carries all of them verbatim.
  //
  // Scoped to the locale that needs it, not applied globally. Japanese and
  // Chinese also run scripts through `\p{L}`, but `ja.md` §1.1 asks for a space
  // at the boundary, so relaxing it there would only weaken a check that
  // currently passes. Absent a locale, stay strict.
  const WORDISH = '[\\p{L}\\p{N}]'
  const agglutinates = String(locale ?? '').toLowerCase().split('-')[0] === 'ko'
  const after = agglutinates
    ? `(?:(?!${WORDISH})|(?=\\p{Script=Hangul}))`
    : `(?!${WORDISH})`
  for (const term of dnt) {
    const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
    const re = new RegExp(`(?<!${WORDISH})${escaped}${after}`, 'u')
    if (re.test(en) && !re.test(tr)) push('dnt-missing', `"${term}" is in the English but not the translation`)
  }

  // The next three rules are RELATIVE to the English, not absolute. Measured
  // against the shipped catalogs, absolute forms produced 142 findings on
  // already-approved translations — and every one was inherited: the English
  // itself carries 77 doubled spaces (JSX text extraction kept the source
  // indentation) and 64 unbalanced brackets (the D1 fragment keys, `'Findings ('`
  // + N + `')'`). Those are SOURCE defects that Phase 3 repairs by de-fragmenting
  // the key; reporting them against the translator is noise that would get this
  // whole check switched off. Fire only on a defect the translation introduces.
  if (tr !== tr.trim() && en === String(en).trim()) {
    push('edge-whitespace', 'leading or trailing whitespace the English does not have')
  }
  if (/ {2}/.test(tr) && !/ {2}/.test(String(en))) {
    push('doubled-space', 'two consecutive spaces the English does not have')
  }

  // Full-width LETTERS and DIGITS only. The whole U+FF01-FF5E block would also
  // catch full-width punctuation, which is correct CJK typography and is what the
  // style guides ask for — flagging `（` here would fight the rule it implements.
  // Absolute rather than relative: English never contains these.
  if (/[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/.test(tr)) {
    push('fullwidth-latin', 'full-width Latin letter or digit')
  }

  // Bracket balance, pooled across widths, as a DELTA against the English. Two
  // calibrations are baked in, both measured against the shipped catalogs:
  //
  //  - DELTA, not balance: the D1 fragment keys (`'Findings ('` + N + `')'`) are
  //    deliberately unbalanced at source, so demanding balance reported 64
  //    findings against already-approved translations.
  //  - POOLED across widths: zh-CN correctly rewrites `(` as `（`, which its own
  //    style guide asks for. Comparing per width called all 60 of those a defect.
  //
  // What remains a defect is MIXING the widths inside one value — `发现 (3）`, the
  // failure `qa.test.ts` exists for — which pooling would hide, so it is checked
  // separately below.
  const FAMILIES = [
    { name: 'round', opens: '(（', closes: ')）' },
    { name: 'square', opens: '[【', closes: ']】' },
    { name: 'curly', opens: '{', closes: '}' },
  ]
  for (const { name, opens, closes } of FAMILIES) {
    const bal = s => countOutside(s, c => opens.includes(c)) - countOutside(s, c => closes.includes(c))
    const dEn = bal(en)
    const dTr = bal(tr)
    if (dEn !== dTr) {
      push('unbalanced-bracket', `${name} bracket balance ${dTr >= 0 ? '+' : ''}${dTr}, English is ${dEn >= 0 ? '+' : ''}${dEn}`)
    }
    // Consistent conversion is fine; using both widths of one family in a single
    // value is the mixed-width pair the QA suite rejects.
    const ascii = countOutside(tr, c => c === opens[0] || c === closes[0]) > 0
    const wide = opens.length > 1 && countOutside(tr, c => c === opens[1] || c === closes[1]) > 0
    if (ascii && wide) push('mixed-width-bracket', `${name} brackets use both ASCII and full-width forms`)
  }

  // A key is a plural form only if its BASE is in `pluralKeys.json`. Shape alone is
  // not enough: the slug generator ends a key with the sentence's last word, so
  // `"…to add one"` becomes `..._add_one` and looked like an impossible `_one` form
  // in three approved zh-CN values. With no registry supplied, stay silent rather
  // than guess — a false blocker here costs more than a missed one.
  if (categories && pluralBases) {
    const m = key.match(/^(.*)_(zero|one|two|few|many|other)$/)
    if (m && pluralBases.includes(m[1]) && !categories.includes(m[2])) {
      push('impossible-plural', `_${m[2]} is not a CLDR category for this locale (${categories.join(', ')})`)
    }
  }

  return findings
}

/**
 * English passthrough is reported separately from `checkValue`'s findings because
 * it is not always wrong — a proper noun or a symbol legitimately survives
 * translation — but a shard where most values match English is a shard nobody
 * translated, and that is worth a hard failure.
 */
export function passthroughRatio(en, tr) {
  const keys = Object.keys(en)
  if (keys.length === 0) return 0
  return keys.filter(k => tr[k] === en[k]).length / keys.length
}

export function renderPrompt(template, slots) {
  let out = template
  for (const [name, value] of Object.entries(slots)) {
    out = out.split(`{{${name}}}`).join(value)
  }
  const leftover = out.match(/\{\{[A-Z_]+\}\}/g)
  if (leftover) throw new Error(`unfilled prompt slot(s): ${[...new Set(leftover)].join(', ')}`)
  return out
}

/** The prompt body between the two markers — the doc's prose is not part of it. */
export function extractPromptBody(doc) {
  const m = doc.match(/^## PROMPT BEGIN\s*$([\s\S]*?)^## PROMPT END\s*$/m)
  if (!m) throw new Error('TRANSLATION-PROMPT.md is missing its "## PROMPT BEGIN" / "## PROMPT END" markers')
  return m[1].trim()
}

// ---------------------------------------------------------------------------
// absent-tolerant inputs
// ---------------------------------------------------------------------------

/**
 * The glossary, style guides and context sidecar each land in their own PR, so a
 * translation run must not hard-fail because one has not merged yet. Same
 * contract `i18n-shard.mjs` uses for the sidecar: degrade, but say so loudly —
 * silently producing context-free, glossary-free output is the exact failure
 * these files exist to prevent.
 */
function readOptional(file, what) {
  if (fs.existsSync(file)) return fs.readFileSync(file, 'utf-8')
  console.warn(`warning: ${path.relative(ROOT, file)} is missing, so ${what}`)
  return null
}

function dntTerms() {
  const raw = readOptional(GLOSSARY_FILE, 'do-not-translate terms cannot be enforced')
  return raw ? (JSON.parse(raw).dnt ?? []) : []
}

function pluralRegistry() {
  return fs.existsSync(PLURAL_KEYS_FILE)
    ? JSON.parse(fs.readFileSync(PLURAL_KEYS_FILE, 'utf-8'))
    : []
}

function pluralCategories(code) {
  try {
    return new Intl.PluralRules(code).resolvedOptions().pluralCategories
  } catch {
    return ['other']
  }
}

// ---------------------------------------------------------------------------
// subcommands
// ---------------------------------------------------------------------------

function cmdPlan(prefix) {
  if (!fs.existsSync(BASELINE_FILE)) {
    console.error(
      `${path.relative(ROOT, BASELINE_FILE)} does not exist.\n`
      + 'It is generated by `node scripts/check-i18n-strings.mjs --update` and is the\n'
      + 'Phase 1 worklist. Without it there is nothing to plan against.',
    )
    process.exit(2)
  }
  const { files } = JSON.parse(fs.readFileSync(BASELINE_FILE, 'utf-8'))
  const rows = Object.entries(files)
    .filter(([f]) => !prefix || f.startsWith(prefix))
    .map(([f, cats]) => ({
      file: f,
      phase1: PHASE1_CATEGORIES.reduce((n, c) => n + (cats[c] ?? 0), 0),
      phase6: PHASE6_CATEGORIES.reduce((n, c) => n + (cats[c] ?? 0), 0),
      cats,
    }))
    .filter(r => r.phase1 > 0)
    .sort((a, b) => b.phase1 - a.phase1)

  const p1 = rows.reduce((n, r) => n + r.phase1, 0)
  const p6 = rows.reduce((n, r) => n + r.phase6, 0)
  console.log(`${rows.length} file(s)${prefix ? ` under ${prefix}` : ''} with Phase 1 work`)
  console.log(`  Phase 1 (this phase):    ${p1}`)
  console.log(`  Phase 6 (deferred):      ${p6}`)
  console.log(`  x ${TARGET_LANGUAGES.length} locales:          ${p1 * TARGET_LANGUAGES.length} translated values\n`)
  for (const r of rows.slice(0, 40)) {
    const detail = Object.entries(r.cats)
      .filter(([c]) => c !== 'total')
      .map(([c, n]) => `${c}:${n}`)
      .join(' ')
    console.log(`  ${String(r.phase1).padStart(4)} p1  ${String(r.phase6).padStart(4)} p6  ${r.file}`)
    console.log(`             ${detail}`)
  }
  if (rows.length > 40) console.log(`  … and ${rows.length - 40} more`)
}

function cmdEmit(baseDir, locales) {
  const enDir = path.join(baseDir, 'en')
  if (!fs.existsSync(enDir)) {
    console.error(
      `${enDir} does not exist.\n`
      + `Run \`node scripts/i18n-shard.mjs split ${enDir}\` first — that is what produces\n`
      + 'the English shards and their translator-context sidecars.',
    )
    process.exit(2)
  }
  const template = extractPromptBody(fs.readFileSync(PROMPT_FILE, 'utf-8'))
  const dnt = dntTerms()
  const pluralBases = pluralRegistry()
  const shards = fs.readdirSync(enDir)
    .filter(f => /^shard-\d+\.json$/.test(f))
    .sort()
  if (shards.length === 0) {
    console.error(`no shard-NN.json found in ${enDir}`)
    process.exit(2)
  }

  let written = 0
  for (const { code, label } of locales) {
    const categories = pluralCategories(code)
    const style = readOptional(path.join(STYLE_DIR, `${code}.md`), `${code} has no style guide to follow`)
    const existing = fs.existsSync(path.join(LOCALES_DIR, `${code}.json`))
      ? flatten(JSON.parse(fs.readFileSync(path.join(LOCALES_DIR, `${code}.json`), 'utf-8')))
      : {}
    const examples = Object.entries(existing)
      .filter(([, v]) => typeof v === 'string' && v.length > 2 && v.length < 60)
      .slice(0, 12)
    const outDir = path.join(baseDir, 'prompts', code)
    fs.mkdirSync(outDir, { recursive: true })

    for (const shard of shards) {
      const stem = shard.replace(/\.json$/, '')
      const ctxFile = path.join(enDir, `${stem}.context.json`)
      const ctx = fs.existsSync(ctxFile) ? fs.readFileSync(ctxFile, 'utf-8') : null
      // Re-key for THIS locale's plural categories, so the prompt asks for the
      // forms it can select and none it cannot. See `localiseShard`.
      const source = localiseShard(
        JSON.parse(fs.readFileSync(path.join(enDir, shard), 'utf-8')),
        pluralBases,
        categories,
      )
      const body = renderPrompt(template, {
        LOCALE: code,
        LANGUAGE_LABEL: label,
        PLURAL_CATEGORIES: categories.join(', '),
        STYLE_GUIDE: style ?? '_(no style guide has landed for this locale yet — apply the general rules above.)_',
        DNT_TERMS: dnt.length
          ? dnt.map(t => `- \`${t}\``).join('\n')
          : '_(no glossary has landed yet — keep product names and proper nouns in Latin script.)_',
        CONTEXT: ctx
          ? `\`\`\`json\n${ctx.trim()}\n\`\`\``
          : '_(no context entries apply to this shard.)_',
        SHARD_JSON: `\`\`\`json\n${JSON.stringify(source, null, 2)}\n\`\`\``,
        EXAMPLES: examples.length
          ? `\`\`\`json\n${JSON.stringify(Object.fromEntries(examples), null, 2)}\n\`\`\``
          : '_(this locale has no catalog yet — you are establishing the terminology.)_',
      })
      fs.writeFileSync(path.join(outDir, `${stem}.prompt.md`), `${body}\n`)
      written += 1
    }
    console.log(`${code}: ${shards.length} prompt(s), plurals [${categories.join(' ')}] -> ${path.relative(process.cwd(), outDir)}`)
  }
  console.log(`\n${written} prompt(s) for ${locales.length} locale(s). Answers go in <baseDir>/<locale>/shard-NN.json.`)
}

/**
 * Character ranges covered by a placeholder, tag or nesting reference.
 *
 * Deliberately NOT implemented as `replace(/<[^>]+>/g, '')`. That shape is a
 * multi-character sanitizer — CodeQL flags it high severity, and correctly in
 * general, because removing `<…>` once can splice a new `<script` out of
 * `<scr<x>ipt`. Nothing here is sanitising untrusted markup for rendering; the
 * only question is which offsets to ignore while counting brackets, and offsets
 * answer it without rewriting the string at all.
 */
export function maskedRanges(value) {
  const ranges = []
  for (const re of [/\{\{[^}]*\}\}/g, /<\/?[^>]+>/g, /\$t\([^)]*\)/g]) {
    for (const m of String(value).matchAll(re)) ranges.push([m.index, m.index + m[0].length])
  }
  return ranges
}

/** Count characters satisfying `pred` that lie OUTSIDE any masked range. */
export function countOutside(value, pred) {
  const s = String(value)
  const ranges = maskedRanges(s)
  let n = 0
  for (let i = 0; i < s.length; i += 1) {
    if (ranges.some(([a, b]) => i >= a && i < b)) continue
    if (pred(s[i])) n += 1
  }
  return n
}

/**
 * Re-key an English shard for one locale's CLDR plural categories.
 *
 * English carries `x_one` + `x_other`. Sending that verbatim asks for exactly the
 * wrong set: zh-CN (categories: `other`) is asked for an `_one` form it can never
 * select — and `verify` then rejects the compliant answer — while ru
 * (`one/few/many/other`) is never asked for `_few`/`_many` at all, so the
 * documented pipeline cannot produce a correct Russian plural.
 *
 * `other` seeds any form the English does not carry: it is the one category every
 * locale has, so it is always present to translate from.
 */
export function localiseShard(shard, pluralBases, categories) {
  const bases = new Set(pluralBases)
  const out = {}
  const grouped = new Map()
  for (const [k, v] of Object.entries(shard)) {
    const m = k.match(/^(.*)_(zero|one|two|few|many|other)$/)
    if (m && bases.has(m[1])) {
      if (!grouped.has(m[1])) grouped.set(m[1], {})
      grouped.get(m[1])[m[2]] = v
    } else {
      out[k] = v
    }
  }
  for (const [base, forms] of grouped) {
    const seed = forms.other ?? Object.values(forms)[0]
    for (const cat of categories) out[`${base}_${cat}`] = forms[cat] ?? seed
  }
  return Object.fromEntries(Object.keys(out).sort().map(k => [k, out[k]]))
}

function cmdVerify(baseDir, code) {
  const enDir = path.join(baseDir, 'en')
  const trDir = path.join(baseDir, code)
  for (const [dir, hint] of [[enDir, 'run `i18n-shard.mjs split` first'], [trDir, 'no filled shards for this locale']]) {
    if (!fs.existsSync(dir)) {
      console.error(`${dir} does not exist — ${hint}.`)
      process.exit(2)
    }
  }
  const dnt = dntTerms()
  const categories = pluralCategories(code)
  const pluralBases = pluralRegistry()
  const read = f => JSON.parse(fs.readFileSync(f, 'utf-8'))
  const shards = fs.readdirSync(enDir).filter(f => /^shard-\d+\.json$/.test(f)).sort()

  const PASSTHROUGH_LIMIT = 0.5
  const findings = []
  const overRatio = []
  let keyCount = 0
  let identical = 0

  // PER SHARD, not aggregated. One untranslated shard sitting among translated
  // ones stays under an aggregate 50% and would pass, after which `merge` writes
  // English into the catalog — which is exactly the state this gate exists to
  // stop. A shard is the unit a translator fills, so it is the unit to judge.
  for (const shard of shards) {
    const expected = localiseShard(read(path.join(enDir, shard)), pluralBases, categories)
    const trFile = path.join(trDir, shard)
    if (!fs.existsSync(trFile)) {
      findings.push({ key: shard, rule: 'missing-shard', detail: 'no translation for this shard' })
      continue
    }
    const tr = read(trFile)
    keyCount += Object.keys(expected).length
    for (const k of Object.keys(expected)) {
      if (!(k in tr)) findings.push({ key: k, rule: 'missing-key', detail: `absent from ${shard}` })
    }
    for (const k of Object.keys(tr)) {
      if (!(k in expected)) findings.push({ key: k, rule: 'unknown-key', detail: `not expected for ${code} in ${shard}` })
    }
    for (const k of Object.keys(expected)) {
      if (k in tr) {
        findings.push(...checkValue({
          key: k, en: expected[k], tr: tr[k], dnt, categories, pluralBases, locale: code,
        }))
      }
    }
    const ratio = passthroughRatio(
      Object.fromEntries(Object.entries(expected).filter(([k]) => k in tr)),
      tr,
    )
    identical += Object.keys(expected).filter(k => tr[k] === expected[k]).length
    if (ratio > PASSTHROUGH_LIMIT) overRatio.push({ shard, ratio })
  }

  const overall = keyCount ? identical / keyCount : 0
  console.log(
    `[${code}] ${shards.length} shard(s), ${keyCount} key(s), plurals [${categories.join(' ')}], `
    + `${findings.length} finding(s), ${(overall * 100).toFixed(1)}% identical to English`,
  )
  if (findings.length) {
    const byRule = {}
    for (const f of findings) (byRule[f.rule] ??= []).push(f)
    for (const [rule, items] of Object.entries(byRule).sort((a, b) => b[1].length - a[1].length)) {
      console.error(`\n  ${rule} (${items.length})`)
      for (const it of items.slice(0, 8)) console.error(`    ${it.key}: ${it.detail}`)
      if (items.length > 8) console.error(`    … and ${items.length - 8} more`)
    }
  }
  for (const { shard, ratio } of overRatio) {
    console.error(
      `\n  passthrough: ${shard} is ${(ratio * 100).toFixed(1)}% byte-identical to English, above the `
      + `${PASSTHROUGH_LIMIT * 100}% limit.\n  That is the shape of a shard nobody translated.`,
    )
  }
  if (findings.length || overRatio.length) process.exit(1)
  console.log(`OK: ${code} is ready to merge.`)
}

/**
 * Merge translated keys into an existing catalog.
 *
 * INSERT-ONLY by default. A translation run is normally adding keys, and every
 * value already in the catalog may carry a contributor edit — the catalogs are
 * AI-generated *plus* human correction, so silently replacing 4000 existing
 * values because someone sharded the full corpus instead of the new keys would
 * discard exactly the work that is hardest to reproduce. Overwriting is a
 * deliberate act (`--overwrite`), not a default.
 *
 * Returns `{ merged, skipped }` so the caller can report what it declined to
 * touch rather than leaving it invisible.
 */
export function mergeCatalog(existing, additions, { overwrite = false } = {}) {
  const out = structuredClone(existing)
  const skipped = []
  let merged = 0
  for (const [key, value] of Object.entries(additions)) {
    const parts = key.split('.')
    const leaf = parts.pop()
    for (const seg of [...parts, leaf]) {
      if (seg === '__proto__' || seg === 'constructor' || seg === 'prototype') {
        throw new Error(`Refusing to nest key '${key}': unsafe object-key segment.`)
      }
    }
    let node = out
    for (const p of parts) {
      if (typeof node[p] !== 'object' || node[p] === null || Array.isArray(node[p])) node[p] = {}
      node = node[p]
    }
    if (!overwrite && Object.prototype.hasOwnProperty.call(node, leaf)) {
      skipped.push(key)
      continue
    }
    node[leaf] = value
    merged += 1
  }
  return { catalog: out, merged, skipped }
}

export function sortDeep(obj) {
  if (obj === null || typeof obj !== 'object' || Array.isArray(obj)) return obj
  return Object.fromEntries(Object.keys(obj).sort().map(k => [k, sortDeep(obj[k])]))
}

function cmdMerge(baseDir, locales, { overwrite }) {
  const results = []
  for (const { code } of locales) {
    const dir = path.join(baseDir, code)
    const target = path.join(LOCALES_DIR, `${code}.json`)
    if (!fs.existsSync(dir)) {
      results.push({ code, ok: false, why: 'no shard directory' })
      continue
    }
    if (!fs.existsSync(target)) {
      results.push({ code, ok: false, why: `${path.relative(ROOT, target)} does not exist` })
      continue
    }
    const additions = Object.assign(
      {},
      ...fs.readdirSync(dir)
        .filter(f => f.endsWith('.json') && !f.endsWith('.context.json'))
        .sort()
        .map(f => flatten(JSON.parse(fs.readFileSync(path.join(dir, f), 'utf-8')))),
    )
    const empty = Object.entries(additions).filter(([, v]) => typeof v !== 'string' || !v.trim())
    if (empty.length) {
      // Fail closed. A blank value merged into a catalog renders as nothing at
      // all — worse than the English fallback it replaces — and
      // `returnEmptyString: false` makes it invisible in review.
      results.push({ code, ok: false, why: `${empty.length} empty value(s), e.g. ${empty[0][0]}` })
      continue
    }
    const before = JSON.parse(fs.readFileSync(target, 'utf-8'))
    const beforeCount = Object.keys(flatten(before)).length
    const { catalog, merged, skipped } = mergeCatalog(before, additions, { overwrite })
    const afterCount = Object.keys(flatten(catalog)).length
    fs.writeFileSync(target, `${JSON.stringify(sortDeep(catalog), null, 2)}\n`)
    results.push({
      code,
      ok: true,
      why: `${merged} key(s) merged, ${beforeCount} -> ${afterCount}`
        + (skipped.length ? `, ${skipped.length} left alone (already translated)` : ''),
      skipped,
    })
  }

  for (const r of results) console.log(`${r.ok ? 'ok  ' : 'FAIL'} ${r.code.padEnd(6)} ${r.why}`)
  const withSkips = results.filter(r => r.ok && r.skipped?.length)
  if (withSkips.length && !overwrite) {
    const sample = withSkips[0].skipped.slice(0, 5)
    console.log(
      `\nKeys already present were left as they are — a catalog value may carry a\n`
      + `contributor edit, and replacing it is a deliberate act. Pass --overwrite to\n`
      + `replace them. Example: ${sample.join(', ')}`,
    )
  }
  const failed = results.filter(r => !r.ok)
  console.log(`\n${results.length - failed.length}/${results.length} locale(s) merged.`)
  if (failed.length) {
    console.error(
      `\n${failed.length} locale(s) did not merge: ${failed.map(r => r.code).join(', ')}.\n`
      + 'catalogParity.test.ts requires every locale, so this commit is not shippable yet.',
    )
    process.exit(1)
  }
}

// ---------------------------------------------------------------------------
// CLI
// ---------------------------------------------------------------------------

function resolveLocales(argv) {
  const arg = argv.find(a => a.startsWith('--locales='))
  if (!arg) return TARGET_LANGUAGES
  const want = arg.slice('--locales='.length).split(',').map(s => s.trim()).filter(Boolean)
  const known = new Map(TARGET_LANGUAGES.map(l => [l.code, l]))
  const unknown = want.filter(c => !known.has(c))
  if (unknown.length) {
    console.error(
      `unknown locale(s): ${unknown.join(', ')}\n`
      + `shipped targets: ${TARGET_LANGUAGES.map(l => l.code).join(', ')}`,
    )
    process.exit(2)
  }
  return want.map(c => known.get(c))
}

function main(argv) {
  const [cmd, ...rest] = argv
  const positional = rest.filter(a => !a.startsWith('-'))

  // An unrecognised flag must not fall through to a run that writes files with
  // default settings — the same footgun `i18n-codemod.mjs` closes.
  const KNOWN = ['--locales=', '--locale=', '--overwrite']
  const unknown = rest.filter(a => a.startsWith('-') && !KNOWN.some(k => a.startsWith(k)))
  if (unknown.length) {
    console.error(`unknown flag(s): ${unknown.join(', ')}\nknown: ${KNOWN.join(' ')}`)
    process.exit(2)
  }

  switch (cmd) {
    case 'plan':
      return cmdPlan(positional[0])
    case 'emit':
      if (!positional[0]) return usage('emit needs a <baseDir>')
      return cmdEmit(positional[0], resolveLocales(rest))
    case 'verify': {
      const localeArg = rest.find(a => a.startsWith('--locale='))
      const code = localeArg?.slice('--locale='.length)
      if (!positional[0] || !code) return usage('verify needs a <baseDir> and --locale=<tag>')
      if (!TARGET_LANGUAGES.some(l => l.code === code)) {
        console.error(`unknown locale: ${code}\nshipped targets: ${TARGET_LANGUAGES.map(l => l.code).join(', ')}`)
        process.exit(2)
      }
      return cmdVerify(positional[0], code)
    }
    case 'merge':
      if (!positional[0]) return usage('merge needs a <baseDir>')
      return cmdMerge(positional[0], resolveLocales(rest), { overwrite: rest.includes('--overwrite') })
    default:
      return usage(cmd ? `unknown command: ${cmd}` : 'no command given')
  }
}

function usage(problem) {
  console.error(
    `${problem}\n\n`
    + 'usage:\n'
    + '  node scripts/i18n-translate.mjs plan [pathPrefix]\n'
    + '  node scripts/i18n-translate.mjs emit <baseDir> [--locales=a,b]\n'
    + '  node scripts/i18n-translate.mjs verify <baseDir> --locale=<tag>\n'
    + '  node scripts/i18n-translate.mjs merge <baseDir> [--locales=a,b] [--overwrite]\n',
  )
  process.exit(2)
}

// Only dispatch when run as a script, so the helpers above stay importable by the
// test suite without executing anything.
if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  main(process.argv.slice(2))
}
