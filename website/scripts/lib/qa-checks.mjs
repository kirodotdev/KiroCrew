/**
 * Catalog QA check definitions — the single definition, shared by both consumers.
 *
 * Two gates need exactly these predicates and must never disagree about them:
 *
 *   - `src/i18n/qa.test.ts` counts violations across the whole catalog set and holds
 *     the frozen debt as a per-check ceiling.
 *   - `scripts/check-source-strings.mjs` runs them at ZERO tolerance over the values
 *     this branch added or changed, which is what keeps the ceiling from being a way
 *     to swap one malformed string for another.
 *
 * They used to live inside the test file, on the stated reasoning that a check and its
 * assertions cannot drift apart if there is only one copy. That reasoning is why this
 * is a module rather than a second copy: the diff-scoped gate is a script, so the
 * shared definition has to be importable from plain Node. Both consumers import from
 * here and neither redefines anything.
 */

/**
 * Interpolation placeholders are removed before any punctuation check. `{{count}}`
 * contains braces that would otherwise register as an unbalanced pair.
 */
export const stripInterpolation = (v) => v.replace(/\{\{[^}]*\}\}/g, '')

export const DELIMITER_PAIRS = [
  ['(', ')'],
  ['[', ']'],
  ['（', '）'],
  ['【', '】'],
  ['「', '」'],
]

/**
 * Values that are a single connector or morpheme. These cannot be translated in
 * isolation — `'s'` is an English plural suffix and `'repl'` is the stem of
 * "replies", so every language ships them verbatim and the UI renders English.
 */
export const CONNECTORS = new Set([
  'and', 'or', 'of', 'to', 'in', 'on', 'for', 'with', 'by', 'at', 'from',
  'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be',
  's', 'es', 'y', 'ies', 'repl',
])

/** Fullwidth digits and Latin letters. Fullwidth *punctuation* is correct in CJK; these are not. */
export const FULLWIDTH_ALPHANUMERIC = /[\uFF10-\uFF19\uFF21-\uFF3A\uFF41-\uFF5A]/

/**
 * The curly double-quote pair a locale actually uses, keyed by language.
 *
 * Pairing curly quotes is locale-specific, and getting it wrong in either direction is a
 * bug. Most locales open with U+201C `“` and close with U+201D `”`. The low-high locales
 * — German here, plus Polish, Czech, Croatian, Hungarian and others if they are ever
 * shipped — open with U+201E `„` and close with U+201C `“`. So German's correct
 * `„Weiter“` has one `“` and no `”`, which an English-shaped rule reports as unbalanced.
 *
 * Guillemets (`«` `»`, used by French and Russian) are deliberately NOT checked: they
 * are a separate pair with their own spacing rules, and no shipped catalog has shown a
 * defect in them. That is a stated false-negative class, not an oversight.
 */
export const QUOTE_PAIRS = {
  de: ['\u201E', '\u201C'],
}
export const DEFAULT_QUOTE_PAIR = ['\u201C', '\u201D']

const count = (haystack, needle) => haystack.split(needle).length - 1

export const CHECKS = [
  {
    id: 'unbalanced-delimiter',
    describe: 'brackets and parentheses must be balanced within a single value',
    violates: (v) => {
      const t = stripInterpolation(v)
      return DELIMITER_PAIRS.some(([open, close]) => count(t, open) !== count(t, close))
    },
  },
  {
    id: 'odd-quote-count',
    describe: 'quotation marks must pair within a single value',
    violates: (v, lang) => {
      const t = stripInterpolation(v)
      // Curly quotes are DIRECTIONAL, so parity over their sum is the wrong test: the
      // previous `(count(“) + count(”)) % 2` passed on any even total, so `“click “here`
      // — two openers, no closer — was reported as balanced. Compare the locale's opener
      // against its closer instead, which catches an odd total AND an even-but-mismatched
      // one. Straight `"` is non-directional, so parity is all that can be checked there.
      const [open, close] = QUOTE_PAIRS[lang] ?? DEFAULT_QUOTE_PAIR
      return count(t, open) !== count(t, close) || count(t, '"') % 2 === 1
    },
  },
  {
    id: 'edge-whitespace',
    describe: 'no leading or trailing space or tab',
    // U+00A0 is excluded deliberately: a non-breaking space is a glyph the copy
    // asked for, not accidental padding.
    violates: (v) => v !== v.replace(/^[ \t\n\r]+/, '').replace(/[ \t\n\r]+$/, ''),
  },
  {
    id: 'doubled-space',
    describe: 'no run of two or more spaces',
    // A whitespace run containing a newline is indentation carried over from a
    // multi-line JSX literal; it collapses to one space when rendered and is not
    // a defect. Only newline-free runs are accidental.
    violates: (v) =>
      [...v.matchAll(/[ \t\n\r]{2,}/g)].some((m) => !m[0].includes('\n') && !m[0].includes('\r')),
  },
  {
    id: 'bare-connector',
    describe: 'a value must not be a lone connector word or morpheme',
    violates: (v) => CONNECTORS.has(v.trim().toLowerCase().replace(/[.,;:!?]+$/, '')),
  },
  {
    id: 'fullwidth-alphanumeric',
    describe: 'CJK catalogs must not store fullwidth Latin letters or digits',
    // W3C CLReq: "when storing text, avoid the fullwidth alphabetic and numeric
    // characters of that block; leave it to the layout engine."
    violates: (v) => FULLWIDTH_ALPHANUMERIC.test(v),
  },
]

/** Flatten a nested catalog to `dotted.key` → string. */
export function flatten(obj, prefix = '') {
  const out = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

/** `lang:key` — the unit a violation is reported at. */
export const site = (lang, key) => `${lang}:${key}`

/**
 * Violations among the values a branch ADDED or CHANGED, at zero tolerance.
 *
 * This is the guard the per-site allowlist used to provide. A count-based ceiling
 * cannot tell "640 → 639, one fixed" from "640 → 640, one fixed and one broken", so
 * without this a cleanup could pay for a regression. Scoping to the diff needs no
 * stored ledger, which is the whole reason the ceilings had to be relaxed: nothing
 * here can conflict between branches.
 *
 * **Inherited defects are not the translator's bug.** If the English source for a key
 * trips the same check, a translation of it trips the check too — `'Skills ('` cannot
 * be translated into balanced Russian. Those are the frozen fragments the phase work
 * is removing, counted once against English, so a non-English value is only flagged
 * when English is clean. `translateDriver.test.ts` measures the alternative: run
 * absolute against approved catalogs, the whitespace, bracket and plural rules
 * produced 142, 150 and 115 findings, every one inherited.
 */
export function changedValueFindings({ lang, base, head, enHead = {}, checks = CHECKS }) {
  const findings = []
  for (const [key, value] of Object.entries(head)) {
    if (base[key] === value) continue
    for (const check of checks) {
      if (!check.violates(value, lang)) continue
      const source = enHead[key]
      if (lang !== 'en' && source !== undefined && check.violates(source, 'en')) continue
      findings.push({ lang, key, value, check })
    }
  }
  return findings
}
