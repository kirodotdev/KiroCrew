/**
 * Detector for FULLY HARDCODED plural glue — the count-adjacent template-literal
 * shape with no `i18nT()` anywhere in it:
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
 * ## What matches
 *
 * Inside one template literal: an interpolated expression (the displayed
 * count), then literal text ending in a letter (the noun the suffix glues to),
 * then a ternary interpolation yielding exactly `'s'` or `''`. All three
 * comparison spellings found in this codebase are covered: `> 1`, `!== 1`, and
 * the inverted `=== 1 ? '' : 's'`.
 *
 * The two expressions are deliberately NOT required to be the same: a suffix
 * driven by a different variable than the number on display is the same defect
 * (a plural form chosen in JS), plus a count/form disagreement on top.
 *
 * ## Known limits, accepted
 *
 * Purely lexical, like every pattern in the codemod. Whitespace around the
 * operator and ternary tokens is tolerated and either quote style matches, so
 * a compact or reformatted spelling of the same glue cannot slip past — but an
 * expression containing braces (an object literal) will not match. The scan is
 * a growth ratchet, not an exhaustive census; a miss keeps the count low,
 * never fails anyone.
 */

export const HARDCODED_PLURAL_PATTERNS = [
  /\$\{([^{}]+?)\}([^`$]*[A-Za-z])\$\{([^{}]+?)\s*>\s*1\s*\?\s*(['"])s\4\s*:\s*(['"])\5\s*\}/g,
  /\$\{([^{}]+?)\}([^`$]*[A-Za-z])\$\{([^{}]+?)\s*!==\s*1\s*\?\s*(['"])s\4\s*:\s*(['"])\5\s*\}/g,
  /\$\{([^{}]+?)\}([^`$]*[A-Za-z])\$\{([^{}]+?)\s*===\s*1\s*\?\s*(['"])\4\s*:\s*(['"])s\5\s*\}/g,
]

/**
 * Scan one file's source text.
 *
 * @param {string} source
 * @returns {{ index: number, line: number, text: string }[]} one entry per
 *   match, sorted by position; `line` is 1-based so a report can print
 *   `file:line` an editor can jump to.
 */
export function findHardcodedPluralSites(source) {
  const sites = []
  for (const re of HARDCODED_PLURAL_PATTERNS) {
    re.lastIndex = 0
    for (const m of source.matchAll(re)) {
      sites.push({
        index: m.index,
        line: source.slice(0, m.index).split('\n').length,
        text: m[0],
      })
    }
  }
  return sites.sort((a, b) => a.index - b.index)
}
