/**
 * Index-alignment helpers for the reminder parsers.
 *
 * ## The rule these exist to enforce
 *
 * Both parsers locate schedule words with regexes and record `{start, end}` spans
 * taken from `RegExp.index`. Those spans are later used to blank the matched ranges
 * out of the **original** typed text (`cleanText`), so the string a span was measured
 * against and the string it is applied to must agree on what index 5 means.
 *
 * `RegExp.index` counts **UTF-16 code units**. Two everyday operations silently break
 * that agreement:
 *
 * 1. **`[...str]` iterates CODE POINTS.** One astral character — any emoji in a typed
 *    reminder — is two code units but one code point, so every later index is off by
 *    one and the wrong characters get blanked.
 * 2. **`str.toLowerCase()` can CHANGE LENGTH.** Turkish `İ` (U+0130) lowercases to
 *    `i` + U+0307 COMBINING DOT ABOVE: one code unit becomes two. Spans measured on
 *    the folded string are then shifted when applied to the original, so
 *    `İlaç at 3pm` saved as `İlaç a`.
 *
 * Both bugs corrupt text the user typed and already saved. This module is the single
 * place that gets it right; the parsers must not hand-roll either operation. Four
 * separate defects of this exact shape were fixed one at a time before it was made
 * structural — fixing the instance left sibling code paths still wrong each time.
 */

/**
 * Split into an array of **UTF-16 code units**, the unit `RegExp.index` counts.
 *
 * Use this instead of `[...str]` (or `Array.from`) anywhere the resulting array is
 * indexed with a regex-derived offset. Surrogate pairs are deliberately split into
 * halves: the halves are rejoined intact by `join('')` as long as neither is dropped,
 * and preserving index alignment is what matters.
 */
export function toUnits(s: string): string[] {
  return s.split('')
}

/**
 * Lowercase **for matching only**, guaranteed to stay index-aligned with the input.
 *
 * A code point is folded only when its lowercase form occupies the same number of code
 * units; otherwise the original is kept. So `İ` stays `İ` — it will not match a
 * lowercase pattern, which is correct and harmless, because every schedule token the
 * parsers look for is ASCII or Han. Losing a match on an exotic character is a
 * non-event; shifting every span after it corrupts the user's saved text.
 *
 * The length guarantee is by CONSTRUCTION, not by assertion: iterating code points and
 * appending either the folded form (same code-unit length) or the original means the
 * total cannot drift. `foldForMatch(s).length === s.length` always holds.
 */
export function foldForMatch(s: string): string {
  let out = ''
  for (const ch of s) {
    const low = ch.toLowerCase()
    out += low.length === ch.length ? low : ch
  }
  return out
}
