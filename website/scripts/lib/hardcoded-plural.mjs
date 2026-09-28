/**
 * Detector for FULLY HARDCODED plural glue — a plural form chosen in
 * JavaScript with no `i18nT()` anywhere in it:
 *
 *     `Retry ${failedIds.length} failed subagent${failedIds.length > 1 ? 's' : ''}`
 *
 * ## Why this exists next to the i18nT-anchored patterns
 *
 * The hard-zero patterns in `i18n-plural-codemod.mjs` key on an `i18nT('key')`
 * call sitting immediately before the plural ternary. That is the INCIDENTAL
 * property of those sites, not the defining one: the defect is that JavaScript
 * chose a plural marker outside the translate call, and a fully hardcoded
 * literal commits it just the same while containing no `i18nT` for those
 * patterns to anchor on. Such a site reports zero to the hard-zero tier, so the
 * class regrows silently and each instance costs a separately filed bug.
 *
 * ## What matches — four spellings of the one defect
 *
 * 1. **Template-literal glue** (`HARDCODED_PLURAL_PATTERNS`): inside one
 *    template literal, an interpolated expression (the displayed count), then
 *    literal text ending in a letter (the noun the suffix glues to), then a
 *    ternary interpolation yielding exactly `'s'` or `''`.
 * 2. **JSX-text glue** (`JSX_TEXT_PLURAL_PATTERNS`): the same shape spelled
 *    directly in JSX text — `{count} noun{count > 1 ? 's' : ''}` — with no
 *    template literal and no `i18nT`. The noun text may not contain braces or
 *    angle brackets, so a match never crosses an element boundary, and it must
 *    end in a letter immediately before the ternary, which is what keeps the
 *    i18nT-adjacent JSX form (owned by the hard-zero tier) from matching here.
 * 3. **String concatenation** (`CONCAT_PLURAL_PATTERNS`): a string literal
 *    ending in a letter, `+`, then a (possibly parenthesised) ternary yielding
 *    exactly `'s'` or `''`.
 * 4. **Whole-word ternary** (`WHOLE_WORD_PLURAL_PATTERNS`): the plural chosen
 *    as two complete words — `n === 1 ? 'line' : 'lines'` — in any expression
 *    position (template interpolation, JSX brace, plain code). To keep a
 *    ternary choosing two UNRELATED words from counting as a plural pair, the
 *    plural arm is required, by regex backreference, to be the s-suffixed form
 *    of the singular arm (`line`/`lines`) or its y→ies form
 *    (`category`/`categories`).
 *
 * All three comparison spellings found in this codebase apply to every
 * variant: `> 1`, `!== 1`, and the inverted `=== 1` (arms flipped).
 *
 * For the glued-suffix spellings the count expression and the ternary
 * expression are deliberately NOT required to be the same: a suffix driven by
 * a different variable than the number on display is the same defect (a plural
 * form chosen in JS), plus a count/form disagreement on top.
 *
 * ## Known limits, accepted
 *
 * Purely lexical, like every pattern in the codemod. Whitespace around the
 * operator and ternary tokens is tolerated and either quote style matches, so
 * a compact or reformatted spelling of the same glue cannot slip past — but an
 * expression containing braces (an object literal) will not match, a JSX-text
 * match will not cross a tag boundary (`<b>{n}</b> item{…}` is unseen), and a
 * whole-word pair with irregular morphology (`'child' : 'children'`) or a
 * suffix other than s/ies is unseen. The concat and template spellings
 * require the noun to be LITERAL text ending in a letter — glue onto a
 * variable or a call result (`noun + (n > 1 ? 's' : '')`,
 * `t('x') + (n > 1 ? 's' : '')`) is unseen. A linear region scan (`scanRegions`)
 * keeps a code sample quoted in a comment from counting for the new
 * spellings, and additionally keeps one quoted in a string or template text
 * from counting as a whole-word site; a sample inside a regex literal still
 * reads as code, and an unpaired apostrophe mis-reads the rest of its own
 * line as a string — both rare, and both err toward not counting. The scan
 * is a growth ratchet, not an exhaustive census; a miss keeps the count low,
 * never fails anyone.
 */

export const HARDCODED_PLURAL_PATTERNS = [
  /\$\{([^{}]+?)\}([^`$]*[A-Za-z])\$\{([^{}]+?)\s*>\s*1\s*\?\s*(['"])s\4\s*:\s*(['"])\5\s*\}/g,
  /\$\{([^{}]+?)\}([^`$]*[A-Za-z])\$\{([^{}]+?)\s*!==\s*1\s*\?\s*(['"])s\4\s*:\s*(['"])\5\s*\}/g,
  /\$\{([^{}]+?)\}([^`$]*[A-Za-z])\$\{([^{}]+?)\s*===\s*1\s*\?\s*(['"])\4\s*:\s*(['"])s\5\s*\}/g,
]

/**
 * Spelling 2: JSX-text glue. `{expr}` then JSX text ending in a letter (no
 * braces, no angle brackets — a match never crosses an element boundary), then
 * a brace ternary yielding `'s'`/`''`. The letter-immediately-before-the-brace
 * requirement is load-bearing twice over: it is the noun the suffix glues to,
 * and it is what excludes both the template-literal spelling (whose ternary
 * brace is preceded by `$`) and the i18nT-adjacent JSX form owned by the
 * hard-zero tier (whose ternary brace is preceded by `}`), so no site is ever
 * billed to two tiers or two patterns.
 */
export const JSX_TEXT_PLURAL_PATTERNS = [
  /\{([^{}]+?)\}([^{}<>]*[A-Za-z])\{([^{}]+?)\s*>\s*1\s*\?\s*(['"])s\4\s*:\s*(['"])\5\s*\}/g,
  /\{([^{}]+?)\}([^{}<>]*[A-Za-z])\{([^{}]+?)\s*!==\s*1\s*\?\s*(['"])s\4\s*:\s*(['"])\5\s*\}/g,
  /\{([^{}]+?)\}([^{}<>]*[A-Za-z])\{([^{}]+?)\s*===\s*1\s*\?\s*(['"])\4\s*:\s*(['"])s\5\s*\}/g,
]

/**
 * Spelling 3: string concatenation. A string literal ending in a letter, `+`,
 * an optionally parenthesised condition, and a ternary yielding `'s'`/`''`.
 * The condition excludes quotes, `,`, `;` and `:` so a lazy match cannot span
 * an earlier string or ternary and pair an unrelated noun with a distant
 * suffix.
 */
export const CONCAT_PLURAL_PATTERNS = [
  /(['"])[^'"\n]*[A-Za-z]\1\s*\+\s*\(?\s*([^\n,;:'"]+?)\s*>\s*1\s*\?\s*(['"])s\3\s*:\s*(['"])\4\s*\)?/g,
  /(['"])[^'"\n]*[A-Za-z]\1\s*\+\s*\(?\s*([^\n,;:'"]+?)\s*!==\s*1\s*\?\s*(['"])s\3\s*:\s*(['"])\4\s*\)?/g,
  /(['"])[^'"\n]*[A-Za-z]\1\s*\+\s*\(?\s*([^\n,;:'"]+?)\s*===\s*1\s*\?\s*(['"])\3\s*:\s*(['"])s\4\s*\)?/g,
]

/**
 * Spelling 4: whole-word ternary. The pair constraint is enforced INSIDE each
 * regex with a backreference — the plural arm must be the singular arm plus
 * `s`, or the singular's stem-`y` swapped for `ies` — so `open ? 'yes' : 'no'`
 * style conditional copy can never count as a plural pair. `=== 1` puts the
 * singular in the `?` arm; `!== 1` and `> 1` put the plural there.
 *
 * Each pattern is anchored on its comparison operator, with no free-form
 * condition prefix. Two reasons, both load-bearing: an unanchored lazy prefix
 * (`[^…]+?`) retries from every character and turns the scan quadratic on a
 * long line, and the operator is the only part of the shape the regex needs —
 * the condition itself carries no constraint. The reported site text therefore
 * starts at the operator. The `>` spellings carry a lookbehind so `=>` (arrow)
 * and `>>` (shift) never read as a comparison.
 */
export const WHOLE_WORD_PLURAL_PATTERNS = [
  // === 1 ? 'line' : 'lines'  /  === 1 ? 'category' : 'categories'
  /===\s*1\s*\?\s*(['"])([A-Za-z]+)\1\s*:\s*(['"])\2s\3/g,
  /===\s*1\s*\?\s*(['"])([A-Za-z]+)y\1\s*:\s*(['"])\2ies\3/g,
  // !== 1 ? 'lines' : 'line'  /  !== 1 ? 'categories' : 'category'
  /!==\s*1\s*\?\s*(['"])([A-Za-z]+)s\1\s*:\s*(['"])\2\3/g,
  /!==\s*1\s*\?\s*(['"])([A-Za-z]+)ies\1\s*:\s*(['"])\2y\3/g,
  // > 1 ? 'lines' : 'line'  /  > 1 ? 'categories' : 'category'
  /(?<![=>])>\s*1\s*\?\s*(['"])([A-Za-z]+)s\1\s*:\s*(['"])\2\3/g,
  /(?<![=>])>\s*1\s*\?\s*(['"])([A-Za-z]+)ies\1\s*:\s*(['"])\2y\3/g,
]

const ALL_PATTERN_GROUPS = [
  HARDCODED_PLURAL_PATTERNS,
  JSX_TEXT_PLURAL_PATTERNS,
  CONCAT_PLURAL_PATTERNS,
  WHOLE_WORD_PLURAL_PATTERNS,
]

/** Region kinds for `scanRegions`. */
const CODE = 0
const STRING = 1
const COMMENT = 2

/**
 * One linear pass classifying every character as CODE, STRING or COMMENT, so
 * a match can be checked against WHERE it starts. Purely lexical, single
 * purpose: keep a code sample quoted in a comment or a string from counting
 * as a live plural site. Deliberately approximate where full tokenization
 * would cost more than the defect it prevents — a regex literal reads as
 * code, and an unpaired apostrophe in JSX text mis-reads the rest of its own
 * line as a string. Both errors are line-scoped ('/" strings close at the
 * newline) and both err toward NOT counting a site, which in a growth
 * ratchet keeps the count low and never fails anyone.
 */
function scanRegions(source) {
  const n = source.length
  const state = new Uint8Array(n) // CODE by default
  let i = 0
  while (i < n) {
    const c = source[i]
    const d = i + 1 < n ? source[i + 1] : ''
    if (c === '/' && d === '/') {
      while (i < n && source[i] !== '\n') state[i++] = COMMENT
    } else if (c === '/' && d === '*') {
      state[i++] = COMMENT
      state[i++] = COMMENT
      while (i < n && !(source[i] === '*' && source[i + 1] === '/')) state[i++] = COMMENT
      if (i < n) { state[i++] = COMMENT; state[i++] = COMMENT }
    } else if (c === "'" || c === '"') {
      state[i++] = STRING
      while (i < n && source[i] !== c && source[i] !== '\n') {
        state[i] = STRING
        if (source[i] === '\\' && i + 1 < n) state[++i] = STRING
        i++
      }
      if (i < n && source[i] === c) state[i++] = STRING
    } else if (c === '`') {
      state[i++] = STRING
      while (i < n) {
        if (source[i] === '\\' && i + 1 < n) { state[i++] = STRING; state[i++] = STRING; continue }
        if (source[i] === '`') { state[i++] = STRING; break }
        if (source[i] === '$' && source[i + 1] === '{') {
          // Interpolation is CODE. Track brace depth and nested plain strings
          // so a '}' inside an arm string does not end the interpolation.
          let depth = 1
          i += 2
          while (i < n && depth > 0) {
            const e = source[i]
            if (e === '{') depth++
            else if (e === '}') depth--
            else if (e === "'" || e === '"') {
              state[i++] = STRING
              while (i < n && source[i] !== e && source[i] !== '\n') {
                state[i] = STRING
                if (source[i] === '\\' && i + 1 < n) state[++i] = STRING
                i++
              }
              if (i < n && source[i] === e) state[i] = STRING
            }
            i++
          }
          continue
        }
        state[i++] = STRING
      }
    } else {
      i++
    }
  }
  return state
}

/**
 * Where a group's match must START to count. The original template-literal
 * group keeps its unfiltered behavior byte-for-byte (its matches begin at
 * `${`, which `scanRegions` files under the template's STRING region). The
 * JSX-text and concat groups reject a start inside a comment. The whole-word
 * group is the strictest — its shape is plain enough to appear verbatim in a
 * comment or a documentation string, so its operator must sit in live code.
 */
function startStateAllowed(group, state) {
  if (group === HARDCODED_PLURAL_PATTERNS) return true
  if (group === WHOLE_WORD_PLURAL_PATTERNS) return state === CODE
  return state !== COMMENT
}

/**
 * Scan one file's source text against every pattern group.
 *
 * The groups are constructed to be mutually exclusive on any one site (the
 * glued-suffix arms require an empty arm the whole-word arms forbid, and the
 * JSX-text noun must sit immediately against the brace where the template
 * spelling has `$` and the i18nT-adjacent form has `}`), so no site is billed
 * twice. The identical-span dedupe below is a backstop for that invariant,
 * not a load-bearing filter.
 *
 * @param {string} source
 * @returns {{ index: number, line: number, text: string }[]} one entry per
 *   match, sorted by position; `line` is 1-based so a report can print
 *   `file:line` an editor can jump to.
 */
export function findHardcodedPluralSites(source) {
  const sites = []
  const seen = new Set()
  const regions = scanRegions(source)
  for (const group of ALL_PATTERN_GROUPS) {
    for (const re of group) {
      re.lastIndex = 0
      for (const m of source.matchAll(re)) {
        if (!startStateAllowed(group, regions[m.index])) continue
        const key = `${m.index}:${m[0].length}`
        if (seen.has(key)) continue
        seen.add(key)
        sites.push({
          index: m.index,
          line: source.slice(0, m.index).split('\n').length,
          text: m[0],
        })
      }
    }
  }
  return sites.sort((a, b) => a.index - b.index)
}
