#!/usr/bin/env node
/**
 * Diff-scoped catalog quality gate. Two passes, both zero-tolerance, neither backed by
 * a stored ledger — so neither can ever conflict between branches.
 *
 * **Pass 1 — new English source strings.** The quality of *new English copy*, before it
 * is ever translated. That is the cheapest possible fix point. A fragment like
 * `'Findings ('` costs one edit if it is caught when it is written, and ten translations
 * plus a re-review if it is caught after the catalogs are generated. Mozilla — the
 * closest analogue, with catalogs in git and no TMS — runs a whole quarantine branch for
 * this; a diff-scoped check gets the mechanical half of it for free.
 *
 * **Pass 2 — every catalog value this branch added or changed, in every language.** This
 * is the guard `qa-allowlist.json` used to provide. `src/i18n/qa.test.ts` holds the
 * frozen debt as a per-check COUNT ceiling rather than a frozen set of 948 sites,
 * because regenerating that set on every fix made it conflict between all parallel
 * branches. A count cannot distinguish "one fixed" from "one fixed and one broken", so
 * the ceiling alone would let a cleanup pay for a regression. Restricting the strict
 * check to changed values closes that at zero storage cost. The check predicates are
 * shared with `qa.test.ts` via `lib/qa-checks.mjs`, so the two cannot disagree.
 *
 * ## Scoping
 *
 * Pass 1 checks only keys absent from `en.json` at the merge base, so the frozen
 * pre-existing violations are irrelevant here and there is nothing to allowlist. Pass 2
 * checks only values that differ from the merge base, and exempts a translation whose
 * English source trips the same check — an unbalanced source cannot have a balanced
 * translation. Base ref comes from `I18N_BASE_REF`, else `origin/main`.
 *
 * ## What is deliberately NOT carved out
 *
 * A PR that only wraps existing hardcoded English is exempt from the *coverage*
 * ratchet — that work is the point of the next phase and must not be penalised. It is
 * NOT exempt here, because these checks are about the SHAPE of the string, and a
 * fragment is a fragment whether it was newly authored or newly lifted out of JSX. If
 * `'Findings ('` is being wrapped, the correct change is to wrap the whole sentence.
 *
 * An earlier version exempted any value that already existed in the catalog under
 * another key, on the theory that this identified a wrap. It does not — a wrapped
 * string is new to the catalog by definition — and it handed a permanent pass to every
 * bad shape that already appeared once. `'Findings ('` and `'s'` both slipped through.
 *
 * Truncation is not checked. Keys are slugs of their value capped at 48 characters, so
 * a long string legitimately produces a key that reads like a fragment.
 *
 * Usage:
 *   node scripts/check-source-strings.mjs
 *   I18N_BASE_REF=origin/main node scripts/check-source-strings.mjs
 */

import { execFileSync } from 'node:child_process'
import * as fs from 'node:fs'
import * as path from 'node:path'
import { fileURLToPath } from 'node:url'

import { changedValueFindings, flatten as qaFlatten } from './lib/qa-checks.mjs'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const REPO = path.resolve(ROOT, '..')
// BOTH English catalogs. `en.manual.json` carries the 89 hand-authored keys, including
// the whole left nav — and hand-authored is exactly where malformed copy is most likely,
// since it never went through the codemod. Comparing only `en.json` left a hole the size
// of the gate's whole promise: new copy added to `en.manual.json` was never checked. The
// pseudolocale generator already merges both, so this now matches it.
const CATALOGS = [
  'website/src/i18n/locales/en.json',
  'website/src/i18n/locales/en.manual.json',
]
const BASE_REF = process.env.I18N_BASE_REF || 'origin/main'

const head = Object.assign(
  {},
  ...CATALOGS.map((c) => qaFlatten(JSON.parse(fs.readFileSync(path.join(REPO, c), 'utf-8')))),
)

let base
try {
  base = {}
  for (const catalog of CATALOGS) {
    let raw
    try {
      raw = execFileSync('git', ['show', `${BASE_REF}:${catalog}`], {
        cwd: REPO,
        encoding: 'utf-8',
        maxBuffer: 32 * 1024 * 1024,
      })
    } catch (err) {
      // A catalog that does not exist at the base ref is legitimately new in its
      // entirety, so treat it as empty rather than skipping the whole gate. Only a
      // missing BASE REF (below) means there is nothing to compare against.
      execFileSync('git', ['rev-parse', '--verify', `${BASE_REF}^{commit}`], { cwd: REPO })
      continue
    }
    Object.assign(base, qaFlatten(JSON.parse(raw)))
  }
} catch {
  // No base to compare against — a shallow clone, or the ref is missing. Skip rather
  // than fail: this gate is about NEW strings, and without a base every string looks
  // new, which would fail every branch for the wrong reason. Note that CI treats a
  // failed base-ref fetch as an error before reaching here, so this path means "run
  // locally without the ref", not "gate silently disabled on a PR".
  console.log(`OK: skipped — cannot read the English catalogs at ${BASE_REF} (shallow clone?).`)
  process.exit(0)
}

const newKeys = Object.keys(head).filter((k) => !(k in base))

/**
 * Checks that apply to a NEW English source string. Each is a shape that makes the
 * string hard or impossible to translate well, and each is cheap to fix at the moment
 * of writing.
 */
const CHECKS = [
  {
    id: 'trailing-connector',
    why: 'ends mid-sentence, so the rest of the sentence lives in another key and the '
      + 'translator cannot reorder them',
    // `prefer-full-sentence` in eslint-plugin-formatjs only triggers on leading or
    // trailing WHITESPACE, so `"Done when:"` passes it. This is the gap.
    test: (v) => /[:\-–—,;/+&(\[]$/.test(v.trimEnd()),
  },
  {
    id: 'leading-connector',
    why: 'starts mid-sentence',
    // Two signals, not one. Leading closing punctuation is unambiguous. A leading
    // connector WORD is not: `The skill this update targets no longer exists, so
    // there is nothing to update.` is a complete message, and the bare word list
    // flagged it for starting with `The`.
    //
    // The discriminator is CAPITALISATION, not punctuation. A fragment continues
    // a sentence that started in a sibling node, so it stays lowercase; a
    // complete sentence or a standalone label starts with a capital. Exempting
    // anything merely punctuated would let `and enable it there to start using
    // it.` through — a real fragment that happens to end in a full stop.
    //
    // Every genuinely fragmented value is still caught: a lowercase joiner by
    // this branch, and one that leads with punctuation or whitespace by the
    // `^[)\].,;:]` branch or by `edge-whitespace`.
    //
    // Known limit: a lowercase standalone label like `in progress` is
    // indistinguishable in shape from the fragment `to confirm`. No string-shape
    // rule separates those; a render-time check (Phase 5) is what can.
    test: (v) => /^[)\].,;:]/.test(v)
      || (!/^[A-Z]/.test(v)
        && /^\s*(and|or|of|to|in|on|for|with|by|at|from|the)\s/i.test(v)),
  },
  {
    id: 'unbalanced-delimiter',
    why: 'half a bracket pair, so the other half is in a sibling key or hardcoded in JSX',
    test: (v) => {
      const t = v.replace(/\{\{[^}]*\}\}/g, '')
      return [['(', ')'], ['[', ']'], ['（', '）']].some(
        ([a, b]) => t.split(a).length !== t.split(b).length,
      )
    },
  },
  {
    id: 'edge-whitespace',
    why: 'leading or trailing space — a fragment joined to a sibling at render time',
    test: (v) => v !== v.replace(/^[ \t]+/, '').replace(/[ \t]+$/, ''),
  },
  {
    id: 'bare-morpheme',
    why: 'a lone connector or suffix cannot be translated in isolation',
    test: (v) => /^(s|es|y|ies|and|or|of|to|the|a|an|is|are|repl)$/i.test(v.trim()),
  },
  {
    id: 'english-plural-suffix',
    why: 'looks like hand-rolled pluralization; use a `{{count}}` key and let CLDR pick the form',
    test: (v) => /\(s\)$|\bs\/es\b/.test(v.trim()),
  },
]

const findings = []
for (const key of newKeys) {
  const value = head[key]
  for (const check of CHECKS) {
    if (check.test(value)) findings.push({ key, value, check })
  }
}

console.log(
  `[source-strings] ${newKeys.length} new key(s) vs ${BASE_REF}, `
  + `${findings.length} finding(s).`,
)

// ---------------------------------------------------------------------------
// Second pass: catalog QA over every value this branch ADDED OR CHANGED, in
// every shipped language, at zero tolerance.
//
// This is the guard that used to be `qa-allowlist.json`'s per-site frozen set.
// That allowlist was replaced by a per-check COUNT ceiling, because a frozen set
// of 948 sites has to be regenerated whenever a string is fixed, which made the
// file conflict between every parallel branch. A count cannot tell "one fixed"
// from "one fixed and one broken", so the ceiling alone would let a cleanup pay
// for a regression.
//
// Scoping the strict check to the diff closes that without storing anything: a
// value that is new or edited on this branch must be clean, whatever the frozen
// counts are. Nothing here can conflict, because there is no ledger.
// ---------------------------------------------------------------------------

/** Shipped catalogs, keyed by language. `en` is the two-file merge the runtime does. */
function catalogFiles() {
  const dir = path.join(ROOT, 'src/i18n/locales')
  const byLang = {}
  for (const file of fs.readdirSync(dir).sort()) {
    if (!file.endsWith('.json')) continue
    // `en-XA` is generated from `en` by `gen-pseudolocale.mjs`, so every defect it
    // has is inherited and `--check` already guards its content byte-for-byte.
    if (file === 'en-XA.json') continue
    const lang = file === 'en.manual.json' ? 'en' : file.slice(0, -'.json'.length)
    ;(byLang[lang] = byLang[lang] || []).push(`website/src/i18n/locales/${file}`)
  }
  return byLang
}

function readFlat(paths, ref) {
  const out = {}
  for (const rel of paths) {
    let raw
    if (ref === null) {
      raw = fs.readFileSync(path.join(REPO, rel), 'utf-8')
    } else {
      try {
        raw = execFileSync('git', ['show', `${ref}:${rel}`], {
          cwd: REPO,
          encoding: 'utf-8',
          maxBuffer: 32 * 1024 * 1024,
        })
      } catch {
        // Absent at the base ref means the whole catalog is new on this branch.
        continue
      }
    }
    Object.assign(out, qaFlatten(JSON.parse(raw)))
  }
  return out
}

const byLang = catalogFiles()
const enHead = readFlat(byLang.en ?? [], null)
const qaFindings = []
for (const [lang, paths] of Object.entries(byLang)) {
  qaFindings.push(
    ...changedValueFindings({
      lang,
      base: readFlat(paths, BASE_REF),
      head: readFlat(paths, null),
      enHead,
    }),
  )
}

console.log(
  `[changed-values] ${qaFindings.length} catalog QA finding(s) among values changed vs ${BASE_REF}.`,
)

if (qaFindings.length > 0) {
  const byCheckId = {}
  for (const f of qaFindings) (byCheckId[f.check.id] = byCheckId[f.check.id] || []).push(f)
  console.error('')
  for (const [id, list] of Object.entries(byCheckId)) {
    console.error(`${id} — ${list[0].check.describe}`)
    for (const f of list) console.error(`    ${f.lang}:${f.key}\n      ${JSON.stringify(f.value)}`)
    console.error('')
  }
  console.error(
    'These values are NEW or EDITED on this branch, so they are not frozen debt and there\n'
    + 'is no ceiling to raise. Fix the copy.\n\n'
    + 'A translation is exempt when the ENGLISH source trips the same check — an unbalanced\n'
    + 'source cannot have a balanced translation. If that is your case and this still fires,\n'
    + 'the English is now clean and the translation needs the same fix.',
  )
}

if (findings.length === 0 && qaFindings.length === 0) {
  process.exit(0)
}

if (findings.length === 0) {
  process.exit(1)
}

const byCheck = {}
for (const f of findings) (byCheck[f.check.id] = byCheck[f.check.id] || []).push(f)
console.error('')
for (const [id, list] of Object.entries(byCheck)) {
  console.error(`${id} — ${list[0].check.why}`)
  for (const f of list) console.error(`    ${f.key}\n      ${JSON.stringify(f.value)}`)
  console.error('')
}
console.error(
  'These are NEW English strings, so fixing them costs one edit each. Left in, each '
  + 'costs nine translations and a re-review later.\n'
  + 'Write the whole sentence in one key and use <Trans> if it contains markup.',
)
process.exit(1)
