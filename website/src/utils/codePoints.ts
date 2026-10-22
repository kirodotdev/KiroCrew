const isHighSurrogate = (code: number) => code >= 0xd800 && code <= 0xdbff
const isLowSurrogate = (code: number) => code >= 0xdc00 && code <= 0xdfff

/** `s.slice(start, end)` with both edges moved off a surrogate pair. A plain
 *  UTF-16 slice through an astral character (an emoji) leaves a lone
 *  surrogate in the text sent to the agent, which is not valid Unicode and
 *  wedges the turn. Edges only ever move inward. */
export function sliceCodePoints(s: string, start: number, end: number = s.length): string {
  const from = snapOffPair(s, Math.max(0, start), 1)
  const to = snapOffPair(s, Math.min(s.length, end), -1)
  return from < to ? s.slice(from, to) : ''
}

/** Move UTF-16 index `i` off the middle of a surrogate pair: `dir` is +1 for
 *  a start edge, -1 for an end edge, so the edge always moves inward. */
export function snapOffPair(s: string, i: number, dir: 1 | -1): number {
  return i > 0 && i < s.length && isHighSurrogate(s.charCodeAt(i - 1)) && isLowSurrogate(s.charCodeAt(i)) ? i + dir : i
}
