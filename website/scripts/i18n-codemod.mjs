#!/usr/bin/env node
/**
 * i18n extraction codemod (one-shot migration tool, kept for re-runs).
 *
 * Rewrites user-facing English string literals in `src/**\/*.tsx` into
 * `t('key')` calls and emits the matching `en.json` catalog. Committed rather
 * than run-and-discarded for three reasons: reviewers audit ONE tool instead of
 * ~600 diffs, an upstream sync that imports new English strings can be
 * re-converted by re-running it, and the skip rules below are the honest record
 * of what was deliberately NOT converted.
 *
 * ## What it converts
 *   - JSX attributes on an allowlist of prop names (`label`, `title`,
 *     `description`, `placeholder`, `aria-label`, …) whose value is a plain
 *     string literal.
 *   - JSX text children containing letters.
 *
 * ## What it deliberately skips (and why)
 *   - **Anything not inside a function body.** A module-level
 *     `const NAV = [{ label: 'Chat' }]` evaluated at import time would freeze
 *     the string in whichever language was active when the module loaded —
 *     silently un-switchable. Converting those safely means restructuring each
 *     constant into a getter and updating its consumers, which is a per-site
 *     judgement call, not a mechanical rewrite.
 *   - Template literals and expressions — no single static key exists.
 *   - Strings with no letters (`'—'`, `'4:3'`, `'%'`), which need no
 *     translation and would only add catalog noise.
 *   - Non-allowlisted props (`className`, `href`, `value`, `type`, …) — these
 *     are identifiers/markup, and translating one silently breaks behaviour.
 *   - `src/pages/scenes/**` (decorative), `**\/*.test.tsx` (assertions must keep
 *     matching literal English), and the i18n module itself.
 *
 * ## Why a standalone `t` and not `useTranslation()`
 * Inserting a hook into ~250 components cannot be done mechanically: strings
 * live in render callbacks, plain helper functions, and non-component modules
 * where a hook call is a rules-of-hooks violation (a build error, not a
 * warning). A standalone translate function is legal in every one of those
 * positions. The cost — it does not subscribe to language changes — is paid
 * once, centrally, by remounting the view tree on language change (the `key` on
 * the routed subtree in `main.tsx`). That trades a rare full re-render for ~250
 * reliable call sites.
 *
 * ## Why the import is aliased to `i18nT`, not `t`
 * `t` is an extremely common local identifier in this codebase — `.map(t => …)`
 * over tabs/turns/tasks/themes, and `const t = …`. A bare `import { t }` is
 * shadowed by those locals, and the codemod's generated call then invokes a
 * `Turn`/`FolderTab` object. That is not hypothetical: the first run produced
 * ~30 `TS2349 This expression is not callable` errors from exactly this.
 * `i18nT` collides with nothing (asserted before writing, below).
 *
 * Usage:
 *   node scripts/i18n-codemod.mjs --dry-run     # report only
 *   node scripts/i18n-codemod.mjs               # full conversion (replaces en.json)
 *   node scripts/i18n-codemod.mjs --merge       # incremental: convert the files
 *                                               # that are still unconverted and
 *                                               # ADD their keys to en.json
 *
 * `--merge` is the mode for an upstream sync / post-rebase top-up, where most of
 * the tree is already converted and a full run would extract only the handful of
 * new strings — replacing a 3.7k-key catalog with ~150. It merges instead, and
 * prunes keys whose source string no longer exists so a renamed string cannot
 * leave an orphan behind.
 */

import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'
import ts from 'typescript'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(__dirname, '..')
const SRC = path.join(ROOT, 'src')
const EN_CATALOG = path.join(SRC, 'i18n/locales/en.json')
const DRY_RUN = process.argv.includes('--dry-run')
const MERGE = process.argv.includes('--merge')

/**
 * Name the translate function is imported as. Deliberately NOT `t` — see the
 * header note; `t` is shadowed by local variables across this codebase.
 */
const T_FN = 'i18nT'

/** Module that exports the translate function. */
const T_MODULE = path.join(SRC, 'i18n/t')

/** Relative specifier for `T_MODULE` from the importing file. */
function importSpecifier(fromFile) {
  let rel = path.relative(path.dirname(fromFile), T_MODULE).replace(/\\/g, '/')
  if (!rel.startsWith('.')) rel = './' + rel
  return rel
}

/** Only these JSX props hold display text. Everything else is markup/identifier. */
const TRANSLATABLE_PROPS = new Set([
  'label', 'title', 'subtitle', 'description', 'placeholder', 'hint',
  'tooltip', 'alt', 'aria-label', 'ariaLabel', 'emptyText', 'confirmText',
  'heading', 'caption', 'summary', 'buttonLabel', 'cta', 'legend',
])

/** Paths excluded wholesale — see the skip rules in the header comment. */
const EXCLUDED = [
  'src/i18n/',
  'src/pages/scenes/',
  'src/test/',
  'src/app-sdk/shared-modules',
  'src/model_registry.json',
]

function isExcluded(rel) {
  return EXCLUDED.some(e => rel.startsWith(e) || rel.includes('/' + e))
    || rel.endsWith('.test.tsx') || rel.endsWith('.test.ts')
}

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(full, out)
    else if (entry.name.endsWith('.tsx')) out.push(full)
  }
  return out
}

/** A string worth translating has at least one letter. */
function hasLetters(s) {
  return /[A-Za-z]/.test(s)
}

/**
 * Named/numeric HTML entities that appear in this codebase's JSX text.
 *
 * Why decoding is REQUIRED, not cosmetic: JSX text is entity-decoded by the
 * compiler, so `Settings &rarr; Voice` renders as `Settings → Voice`. Once that
 * text moves into a JSON catalog it becomes an ordinary JS string interpolated
 * as `{i18nT(...)}` — React escapes it, so an undecoded entity renders as the
 * literal characters `&rarr;`. Decoding at capture time keeps the rendered
 * output byte-identical to before the conversion (which is exactly what the
 * English-identity invariant and the existing DOM assertions require).
 */
const HTML_ENTITIES = {
  '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'",
  '&nbsp;': ' ', '&rarr;': '→', '&larr;': '←', '&uarr;': '↑', '&darr;': '↓',
  '&mdash;': '—', '&ndash;': '–', '&hellip;': '…', '&middot;': '·', '&bull;': '•',
  '&times;': '×', '&divide;': '÷', '&asymp;': '≈', '&ne;': '≠', '&le;': '≤',
  '&ge;': '≥', '&plusmn;': '±', '&deg;': '°', '&ldquo;': '“', '&rdquo;': '”',
  '&lsquo;': '‘', '&rsquo;': '’', '&copy;': '©', '&reg;': '®', '&trade;': '™',
  '&check;': '✓', '&cross;': '✗', '&infin;': '∞', '&para;': '¶', '&sect;': '§',
}

/**
 * Decode HTML entities in captured JSX text.
 *
 * Throws on an unrecognised named entity rather than passing it through: a
 * silent pass-through is precisely the bug this function exists to prevent, so
 * an unknown entity must fail the migration loudly and be added to the table
 * above.
 */
function decodeEntities(s) {
  return s
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(Number(d)))
    .replace(/&#[xX]([0-9a-fA-F]+);/g, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&[a-zA-Z][a-zA-Z0-9]*;/g, m => {
      const decoded = HTML_ENTITIES[m]
      if (decoded === undefined) {
        throw new Error(
          `Unrecognised HTML entity ${m} in JSX text — add it to HTML_ENTITIES in `
          + `scripts/i18n-codemod.mjs (leaving it undecoded would render literally).`,
        )
      }
      return decoded
    })
}

/** camelCase a path segment: 'DisplayPanel' -> 'displayPanel', 'chat-view' -> 'chatView'. */
function camel(seg) {
  const parts = seg.replace(/\.tsx?$/, '').split(/[-_.]/g).filter(Boolean)
  return parts
    .map((p, i) => (i === 0 ? p[0].toLowerCase() + p.slice(1) : p[0].toUpperCase() + p.slice(1)))
    .join('')
}

/** Namespace derived from the file path: src/pages/settings/DisplayPanel.tsx -> pages.settings.displayPanel */
function namespaceFor(rel) {
  return rel.replace(/^src\//, '').split('/').map(camel).join('.')
}

/** Stable, readable leaf key from the English text. */
function leafFor(text) {
  const slug = text
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_|_$/g, '')
    .slice(0, 48)
    .replace(/_$/, '')
  return slug || 'text'
}

/**
 * Is this node inside a function body?
 *
 * The load-bearing skip check: only inside a function does the string get
 * re-evaluated per render, which is what makes runtime language switching work.
 * A module-level initializer would bake in the boot language forever.
 */
function insideFunction(node) {
  for (let p = node.parent; p; p = p.parent) {
    if (
      ts.isFunctionDeclaration(p) || ts.isFunctionExpression(p)
      || ts.isArrowFunction(p) || ts.isMethodDeclaration(p)
      || ts.isGetAccessorDeclaration(p)
    ) return true
  }
  return false
}

/** file -> rewritten contents, flushed only after the catalog validates. */
const pendingSourceWrites = new Map()

const catalog = {}
const stats = {
  files: 0, changedFiles: 0, attrs: 0, texts: 0,
  skippedModuleLevel: 0, skippedDynamic: 0, skippedCollision: 0,
}
/** text -> key, per namespace, so identical strings in a file share one key. */
const keysByNs = new Map()

function assignKey(ns, text) {
  if (!keysByNs.has(ns)) keysByNs.set(ns, new Map())
  const nsMap = keysByNs.get(ns)
  if (nsMap.has(text)) return nsMap.get(text)

  const base = leafFor(text)
  let leaf = base
  let n = 2
  // Distinct strings that slugify identically (e.g. truncation collisions) get
  // a numeric suffix so no key is ever silently overwritten.
  const taken = new Set(nsMap.values())
  while (taken.has(`${ns}.${leaf}`)) leaf = `${base}_${n++}`
  const key = `${ns}.${leaf}`
  nsMap.set(text, key)
  return key
}

/**
 * Object keys that must never be written through a computed path.
 *
 * `__proto__` is the dangerous one: `obj['__proto__'] = {}` REPLACES the
 * prototype instead of creating an own property, so the branch silently vanishes
 * from the catalog (and every later write into it lands on a shared object).
 * `constructor`/`prototype` are rejected for the same class of reason — they
 * shadow builtins and produce a catalog that behaves unlike a plain map.
 *
 * Keys here are derived from source text and file paths, so hitting one is
 * unlikely — but the failure is silent data loss, which is exactly the kind of
 * thing to reject loudly rather than hope about.
 */
const UNSAFE_KEY_SEGMENTS = new Set(['__proto__', 'constructor', 'prototype'])

function setCatalog(key, value) {
  const parts = key.split('.')
  for (const part of parts) {
    if (UNSAFE_KEY_SEGMENTS.has(part)) {
      throw new Error(
        `Refusing to write catalog key '${key}': the segment '${part}' would corrupt `
        + `the catalog object (prototype pollution). Rename the source string.`,
      )
    }
  }
  let cur = catalog
  for (let i = 0; i < parts.length - 1; i++) {
    // `hasOwn` (not `in`/truthiness) so an inherited member can never be
    // mistaken for an existing catalog branch.
    if (!Object.hasOwn(cur, parts[i])
        || typeof cur[parts[i]] !== 'object' || cur[parts[i]] === null) {
      cur[parts[i]] = Object.create(null)
    }
    cur = cur[parts[i]]
  }
  cur[parts[parts.length - 1]] = value
}

/** Escape for embedding inside a single-quoted TS string literal. */
function escapeKey(k) {
  return k.replace(/\\/g, '\\\\').replace(/'/g, "\\'")
}

function processFile(file) {
  const rel = path.relative(ROOT, file).replace(/\\/g, '/')
  if (isExcluded(rel)) return
  stats.files++

  const source = fs.readFileSync(file, 'utf-8')
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const ns = namespaceFor(rel)
  const edits = []

  // Fail loudly if the target name is already bound in this file: a generated
  // `i18nT(...)` would resolve to that binding instead of the import, producing
  // a call on a non-function. TypeScript catches most of these, but a local of
  // type `any`/`unknown` would slip through as a RUNTIME crash. Refusing to
  // convert the file is the safe outcome.
  if (new RegExp(`(?:const|let|var|function)\\s+${T_FN}\\b|\\b${T_FN}\\s*=>`).test(source)) {
    console.warn(`  ! skipped ${rel}: '${T_FN}' is already bound in this file`)
    stats.skippedCollision++
    return
  }

  const visit = node => {
    // --- JSX attribute: label="Foo" / label={'Foo'} ---
    if (ts.isJsxAttribute(node) && node.initializer) {
      const name = node.name.getText(sf)
      if (TRANSLATABLE_PROPS.has(name)) {
        let lit = null
        if (ts.isStringLiteral(node.initializer)) {
          lit = node.initializer
        } else if (
          ts.isJsxExpression(node.initializer)
          && node.initializer.expression
          && ts.isStringLiteral(node.initializer.expression)
        ) {
          lit = node.initializer.expression
        }
        // Decode before the letter test, same reason as the JSX-text branch
        // below. JSX decodes entities in attribute values too, so the catalog
        // must hold the decoded form for rendering to stay identical.
        const text = lit ? decodeEntities(lit.text) : ''
        if (lit && hasLetters(text)) {
          if (!insideFunction(node)) {
            stats.skippedModuleLevel++
          } else {
            const key = assignKey(ns, text)
            setCatalog(key, text)
            edits.push({
              start: node.initializer.getStart(sf),
              end: node.initializer.getEnd(),
              text: `{${T_FN}('${escapeKey(key)}')}`,
            })
            stats.attrs++
          }
        } else if (lit === null && ts.isJsxExpression(node.initializer)) {
          stats.skippedDynamic++
        }
      }
    }

    // --- JSX text child ---
    if (ts.isJsxText(node)) {
      const raw = node.getFullText(sf)
      const trimmed = raw.trim()
      // Decode BEFORE the letter test. `&larr;` has letters inside the ENTITY
      // NAME, so testing the raw text lets it through the "must contain a
      // letter" gate and then decodes to a bare `←` — a pure-symbol key this
      // codemod's own skip rule is meant to exclude (it produced the junk keys
      // `text`/`text_2` = `←`/`→`).
      const decoded = trimmed ? decodeEntities(trimmed) : trimmed
      if (decoded && hasLetters(decoded)) {
        if (!insideFunction(node)) {
          stats.skippedModuleLevel++
        } else {
          const key = assignKey(ns, decoded)
          setCatalog(key, decoded)
          // Preserve the original surrounding whitespace so JSX spacing (and
          // therefore rendered inter-element spaces) is untouched.
          const lead = raw.slice(0, raw.indexOf(trimmed))
          const tail = raw.slice(raw.indexOf(trimmed) + trimmed.length)
          edits.push({
            start: node.getFullStart(),
            end: node.getEnd(),
            text: `${lead}{${T_FN}('${escapeKey(key)}')}${tail}`,
          })
          stats.texts++
        }
      }
    }

    ts.forEachChild(node, visit)
  }
  visit(sf)

  if (edits.length === 0) return
  stats.changedFiles++
  if (DRY_RUN) return

  // Apply back-to-front so earlier offsets stay valid.
  let out = source
  edits.sort((a, b) => b.start - a.start)
  for (const e of edits) out = out.slice(0, e.start) + e.text + out.slice(e.end)

  // Add the import once, after the last top-level import.
  if (!new RegExp(`\\b${T_FN}\\b`).test(source)) {
    const importRe = /^import[\s\S]*?from\s*['"][^'"]+['"];?\s*$/gm
    let last = null
    let m
    while ((m = importRe.exec(out)) !== null) last = m
    const stmt = `import { ${T_FN} } from '${importSpecifier(file)}'`
    if (last) {
      const at = last.index + last[0].length
      out = out.slice(0, at) + '\n' + stmt + out.slice(at)
    } else {
      out = stmt + '\n' + out
    }
  }

  // QUEUED, not written yet — see `flushSourceWrites`. Source rewrites and the
  // catalog write must land together or not at all.
  pendingSourceWrites.set(file, out)
}

/**
 * Apply the queued source rewrites.
 *
 * Called ONLY after the catalog has been validated and written. Writing sources
 * eagerly (as this script used to) makes the fail-closed catalog guard actively
 * harmful: on a re-run over an already-converted tree with a couple of new
 * literals, the sources were rewritten to call `i18nT('new.key')`, the guard then
 * correctly refused the tiny catalog — and the run exited leaving those calls
 * pointing at keys that do not exist, so they render as raw key text in the UI.
 * Reproduced exactly that way before this change.
 *
 * All-or-nothing is the only safe coupling: a converted call site is meaningless
 * without its catalog entry.
 */
function flushSourceWrites() {
  for (const [file, contents] of pendingSourceWrites) {
    fs.writeFileSync(file, contents)
  }
  return pendingSourceWrites.size
}

for (const file of walk(SRC)) processFile(file)

const total = stats.attrs + stats.texts

if (!DRY_RUN) {
  // ── Fail-closed guard: NEVER overwrite a populated catalog with a tiny one ──
  //
  // This codemod is NOT idempotent, and that is the dangerous part. On a second
  // run the sources are already converted, so it extracts ~0 strings — and since
  // the catalog is written FRESH (see below), an unguarded write replaces 3796
  // real keys with `{}`, silently turning the whole dashboard into raw
  // translation keys. That happened once during development.
  //
  // A re-run is only legitimate when it re-extracts a comparable corpus (i.e.
  // run against an UNCONVERTED tree, e.g. after an upstream sync). Anything that
  // would shrink the catalog by more than a quarter is treated as this mistake
  // and refused; delete the catalog explicitly to force a from-scratch rebuild.
  let previousCount = 0
  let previous = {}
  if (fs.existsSync(EN_CATALOG)) {
    previous = JSON.parse(fs.readFileSync(EN_CATALOG, 'utf-8'))
    previousCount = Object.keys(flattenForCount(previous)).length
  }

  // --merge: keep the existing corpus and add this run's keys. Skips the shrink
  // guard by construction (the result can only grow), which is exactly why it is
  // an explicit opt-in flag rather than the default — the default MUST stay
  // fail-closed against the accidental-rerun wipe.
  if (MERGE) {
    const merged = deepMergeCatalog(previous, catalog)
    fs.writeFileSync(EN_CATALOG, JSON.stringify(sortDeep(merged), null, 2) + '\n')
    const written = flushSourceWrites()
    const after = Object.keys(flattenForCount(merged)).length
    console.log(`merged into ${EN_CATALOG}: ${previousCount} -> ${after} keys (${written} files rewritten)`)
    process.exit(0)
  }

  const newCount = Object.keys(flattenForCount(catalog)).length
  if (previousCount > 0 && newCount < previousCount * 0.75) {
    console.error(
      `Refusing to overwrite ${EN_CATALOG}: it holds ${previousCount} keys but this run `
      + `extracted only ${newCount}.\n`
      + `  The sources are probably ALREADY converted — re-running then yields ~0 strings and\n`
      + `  would wipe the catalog (every t() call would render its raw key).\n`
      + `  NOTHING was written — source files are untouched, so no call site is left\n`
      + `  pointing at a missing key. To convert only the new strings, re-run with --merge.\n`
      + `  To rebuild from scratch deliberately: revert src/ to an unconverted state, delete\n`
      + `  ${EN_CATALOG}, and re-run.`,
    )
    process.exit(1)
  }

  // Written FRESH, never merged onto the previous output. Merging would retain
  // keys whose source string has since changed — and because a key is derived
  // from its text, a fixed string yields a NEW key while the stale one lingers
  // (the first run left both `confirm_amp_publish` and `confirm_publish`).
  // Hand-authored strings live in `en.manual.json`, which this never touches.
  fs.writeFileSync(EN_CATALOG, JSON.stringify(sortDeep(catalog), null, 2) + '\n')
  flushSourceWrites()
}

/** Deep-merge catalogs for `--merge` (this run's values win on a leaf collision). */
function deepMergeCatalog(a, b) {
  const out = { ...a }
  for (const [k, v] of Object.entries(b)) {
    const cur = out[k]
    out[k] = v !== null && typeof v === 'object' && cur !== null && typeof cur === 'object'
      ? deepMergeCatalog(cur, v)
      : v
  }
  return out
}

/** Flatten to dotted leaf paths — used only for the size guard above. */
function flattenForCount(obj, prefix = '') {
  const out = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [k, v] of Object.entries(obj)) {
    const dotted = prefix ? `${prefix}.${k}` : k
    if (v !== null && typeof v === 'object') Object.assign(out, flattenForCount(v, dotted))
    else out[dotted] = String(v)
  }
  return out
}

/** Sort keys so the catalog diff is stable across runs. */
function sortDeep(obj) {
  if (obj === null || typeof obj !== 'object') return obj
  const out = {}
  for (const k of Object.keys(obj).sort()) out[k] = sortDeep(obj[k])
  return out
}

console.log(
  `${DRY_RUN ? '[dry-run] ' : ''}scanned ${stats.files} files, changed ${stats.changedFiles}\n`
  + `  converted: ${total} (${stats.attrs} attrs, ${stats.texts} text nodes)\n`
  + `  skipped:   ${stats.skippedModuleLevel} module-level, ${stats.skippedDynamic} dynamic, `
  + `${stats.skippedCollision} name-collision`,
)
