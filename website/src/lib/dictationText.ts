const CLOSING_ASCII_PUNCTUATION = /^[,.;:!?)}\]]/u

/** Preserve authored whitespace and keep unspaced scripts continuous, while
 * preventing independently recognized Latin words from being glued together. */
export function dictationSeparator(before: string, after: string): string {
  if (!before || !after || /\s$/u.test(before) || /^\s/u.test(after)) return ''
  // A dictated insertion before an existing comma, sentence ending or closing
  // bracket must keep that punctuation attached to the inserted words.
  if (CLOSING_ASCII_PUNCTUATION.test(after)) return ''
  if (/[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}]$/u.test(before) ||
      /^[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}，。！？、；：]/u.test(after)) return ''
  return ' '
}

export function joinTranscript(parts: string[]): string {
  return parts.map(part => part.trim()).filter(Boolean)
    .reduce((all, part) => all + dictationSeparator(all, part) + part, '')
}

/** A bounded recent caption, preserving unspaced scripts at the cut. Only a
 * partial word in a spaced script is advanced to the next transcript boundary. */
export function transcriptTail(text: string, maxChars: number): string {
  if (maxChars <= 0) return ''
  let start = Math.max(0, text.length - maxChars)
  // The character budget counts UTF-16 units; never leave half a Han character.
  if (start > 0 && /[\uDC00-\uDFFF]/u.test(text[start])) start++
  const tail = text.slice(start)
  if (!start || (!CLOSING_ASCII_PUNCTUATION.test(tail) &&
      !dictationSeparator(text.slice(0, start), tail))) return tail.trimStart()
  const characters = Array.from(tail)
  for (let i = 1; i < characters.length; i++) {
    // A comma belongs to the word being dropped, not the start of the caption.
    if (!CLOSING_ASCII_PUNCTUATION.test(characters[i]) &&
        !dictationSeparator(characters[i - 1], characters[i])) {
      return characters.slice(i).join('').trimStart()
    }
  }
  return tail
}

export function spliceDictationText(
  base: string,
  text: string,
  caret: { start: number; end: number } | null,
): { value: string; caret: number } {
  // A silent hypothesis must not delete a selected portion of the draft.
  if (!text) return { value: base, caret: caret ? Math.min(caret.start, base.length) : base.length }
  if (!caret) {
    const value = base + dictationSeparator(base, text) + text
    return { value, caret: value.length }
  }
  const before = base.slice(0, Math.min(caret.start, base.length))
  const after = base.slice(Math.min(caret.end, base.length))
  const insert = dictationSeparator(before, text) + text
  return {
    value: before + insert + dictationSeparator(text, after) + after,
    caret: before.length + insert.length,
  }
}
