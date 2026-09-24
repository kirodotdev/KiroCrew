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

/** The one derivation of a dictation splice: the text on each side, the
 *  separators the splice adds, and the selected characters the caret displaces.
 *  The written value and the span a discard has to verify are both built from
 *  this, so the two can never describe different writes. */
function dictationSpliceParts(
  base: string,
  text: string,
  caret: { start: number; end: number } | null,
): { before: string; lead: string; trail: string; after: string; replaced: string } {
  if (!caret) return { before: base, lead: dictationSeparator(base, text), trail: '', after: '', replaced: '' }
  const start = Math.min(caret.start, base.length)
  const end = Math.min(caret.end, base.length)
  const before = base.slice(0, start)
  const after = base.slice(end)
  return {
    before,
    lead: dictationSeparator(before, text),
    trail: dictationSeparator(text, after),
    after,
    replaced: base.slice(start, end),
  }
}

export function spliceDictationText(
  base: string,
  text: string,
  caret: { start: number; end: number } | null,
): { value: string; caret: number } {
  // A silent hypothesis must not delete a selected portion of the draft.
  if (!text) return { value: base, caret: caret ? Math.min(caret.start, base.length) : base.length }
  const { before, lead, trail, after } = dictationSpliceParts(base, text, caret)
  const insert = lead + text
  return { value: before + insert + trail + after, caret: before.length + insert.length }
}

/** Which characters of the written value are the machine's, and what the write
 *  consumed to put them there.
 *
 *  A discard needs all of it. The span is what makes the dictated run
 *  identifiable as a POSITION rather than as a string, so an identical phrase the
 *  user typed elsewhere is never mistaken for it. `replaced` is the selection the
 *  splice deleted: dictating over selected words removes them, so dropping the
 *  span alone would give back a draft the user never wrote. `trail` is the
 *  separator the splice added on the far side, reported apart from the run
 *  because only some writes carry it into the composer -- a later correction
 *  keeps the live tail, separator and all, instead of rebuilding it.
 *
 *  `end` is the insertion point the splice reports as its caret, so a caller that
 *  holds the written value can record this span without measuring it again. */
export function dictationSpliceSpan(
  base: string,
  text: string,
  caret: { start: number; end: number } | null,
): { start: number; end: number; text: string; trail: string; replaced: string } | null {
  if (!text) return null
  const { before, lead, trail, replaced } = dictationSpliceParts(base, text, caret)
  const run = lead + text
  return { start: before.length, end: before.length + run.length, text: run, trail, replaced }
}

/** Whether the written value holds a copy of the run's own word immediately
 *  after the span, separated by whitespace at most.
 *
 *  That is the shape where deleting the machine's copy and deleting the user's
 *  own leave the SAME draft, so the survivor slides into the run's offsets and
 *  reads as the run. The comparison is on the run's word rather than on
 *  `span.text`, and it steps over the whitespace between the two, because the
 *  copy sits past the separator the splice added behind the run -- and past any
 *  the draft already had there.
 *
 *  The copy has to END where the word does. A following word that merely STARTS
 *  with the run is not a copy of it: dictating `send` before the draft's
 *  `sending` leaves two deletions with different results, so the position was
 *  never ambiguous and refusing would strand speech the discard can take.
 *
 *  Only the far side needs this. A copy the draft held BEFORE the span is
 *  identical to the run, so deleting it leaves a value whose common prefix with
 *  the written one runs past the deletion and into the surviving copy: the offset
 *  arithmetic then sees an edit it cannot place wholly on either side of the
 *  span and refuses on its own. */
function abutsIdenticalCopy(
  written: string,
  span: { end: number; text: string },
): boolean {
  const word = span.text.trim()
  if (!word) return false
  let after = span.end
  while (after < written.length && /\s/u.test(written[after])) after++
  if (written.slice(after, after + word.length) !== word) return false
  return !/[\p{L}\p{N}]/u.test(written[after + word.length] ?? '')
}

/** Where a written span sits in a value the user has edited since, or -1 when
 *  that cannot be established UNIQUELY.
 *
 *  Everything between the common prefix and the common suffix of the written
 *  value and the current one is the user's edit. When that edit lies wholly to
 *  one side of the span, the span itself survived and its offset follows by
 *  arithmetic: an edit before it shifts it by the length delta, an edit after it
 *  leaves it where it was. An edit that reaches into the span means those
 *  characters are no longer only the machine's, and there is nothing a discard
 *  may safely take.
 *
 *  Which side the edit fell on is not always decidable, and that is the case
 *  this function exists to refuse. Five shapes make it undecidable, and all are
 *  refused.
 *
 *  Two are insertions that touch the span, one on each side, and they are the same
 *  doubt read twice. The far one BEGINS where the span ends: the typing could have
 *  started a character earlier and rewritten the run's own tail, so selecting a
 *  dictated `plan` and typing `planet` leaves exactly what typing `et` after it
 *  leaves, and the second reading's letters are the user's. The near one ends where
 *  the span lands. The user's own characters run
 *  right up against the run, so their typing having continued through it explains the
 *  pair exactly as well: replacing a dictated `plan` with `new plan` leaves what
 *  typing `new ` in front of it leaves, and under that reading the `plan` in the
 *  composer is theirs. A deletion is not in doubt the same way, having put no
 *  characters in, and neither is an insertion with unchanged text between it and the
 *  span, which would have to be two edits to reach it.
 *
 *  Another is that the prefix and suffix walk finds a single alignment where several
 *  explain the same pair. A dictated phrase that repeats a word the draft goes on
 *  to repeat makes the walk run THROUGH the span: deleting the first copy of the
 *  repeated word leaves a value whose prefix still agrees past the span's end, so
 *  the edit reads as one made beyond the span while it was in fact made in front
 *  of it, and the span is reported whole at an offset where only part of it
 *  survives. Taking that offset deletes the draft the user authored, so the
 *  boundary is slid toward the span for as long as the pair still explains itself,
 *  and "after the span" is concluded only when the furthest reading agrees.
 *
 *  One is an insertion whose own characters repeat the run's: typing `ship ` in
 *  front of a dictated `ship` is indistinguishable from typing ` ship` after it,
 *  so the run's text is found at BOTH offsets and nothing in the value says which
 *  copy the machine wrote. Both candidates are therefore checked, not just the
 *  selected one, and two holding offsets answer -1.
 *
 *  The other is the written value holding a copy of the run's own word
 *  immediately after the span, which is what dictating a word the draft already
 *  continues with produces. Deleting the machine's copy and deleting the user's
 *  own leave the same value, so the survivor slides into the run's offsets and
 *  passes the identity check as the run. Nothing about `cur` can tell those
 *  apart, so this one is decided on `written` alone, and the copy is looked for
 *  past the whitespace between the two -- the separator the splice added behind
 *  the run, plus any the draft already had -- because a duplicated word is
 *  removed together with the space that duplicated it. A copy the draft held
 *  BEFORE the span needs no rule of its own: being identical to the run, its
 *  deletion leaves a value whose common prefix with the written one runs past the
 *  deletion into the surviving copy, and the arithmetic below then places the edit
 *  on neither side and refuses.
 *
 *  Whether an untouched value is spared that refusal depends on `replaced`, not on
 *  the edit. Removing either copy does leave the same draft -- but the discard
 *  REINSERTS what the dictation displaced at the offset it removed from, so when
 *  something was displaced the two copies are not interchangeable and the offset
 *  decides where it lands. An edit that returns the value to byte-identical is
 *  indistinguishable from no edit at all, so a value that still reads as written
 *  cannot be taken as proof the run is where it was put: dictating over a
 *  selection whose word the draft repeats, then editing back to the same bytes
 *  with the two copies' provenance swapped, restores the displaced word one copy
 *  off -- a visible transposition. So the refusal is asked of every value with
 *  something to reinsert, and only a run that displaced NOTHING is spared it on
 *  the strength of the two deletions agreeing.
 *
 *  In both cases the composer keeps what it has, which the user can see and fix,
 *  rather than losing what they typed.
 *
 *  The selected offset is also checked against the span's own text, the same way
 *  the polish path checks its offsets: a mismatch answers -1, never "remove
 *  anyway". */
export function locateDictationSpan(
  written: string,
  cur: string,
  span: { start: number; end: number; text: string; replaced?: string },
): number {
  // Decided before the diff, because no reading of `cur` can resolve it. A value
  // that still reads as written is NOT proof the run is where it was put -- an
  // edit that lands back on the same bytes leaves no trace -- so the only run
  // spared this refusal is one that displaced nothing: there the two deletions
  // agree and no offset has to be trusted, and refusing would leave the speech in
  // a composer whose owner asked for it to go.
  const displaced = (span.replaced ?? '') !== ''
  if ((cur !== written || displaced) && abutsIdenticalCopy(written, span)) return -1
  let head = 0
  while (head < written.length && head < cur.length && written[head] === cur[head]) head++
  let tail = 0
  while (
    tail < written.length - head
    && tail < cur.length - head
    && written[written.length - 1 - tail] === cur[cur.length - 1 - tail]
  ) tail++
  const holds = (at: number) => at >= 0 && cur.slice(at, at + span.text.length) === span.text
  const shifted = span.start + (cur.length - written.length)
  // What the edit took out and what it put in, at the boundary the walk found.
  const removed = written.length - tail - head
  const inserted = cur.length - tail - head
  let at = -1
  if (removed === 0 && inserted === 0) at = span.start         // nothing was edited
  else if (head + removed <= span.start) {
    // The edit is before the span, so the span slid by the length delta -- unless the
    // user's own inserted characters run right up against where it landed. Then the
    // same pair of values is equally explained by their typing having continued
    // THROUGH the run: replacing a dictated `plan` with `new plan` leaves exactly
    // what typing `new ` in front of it leaves, and in the first reading the `plan`
    // in the composer is the user's own. Taking it deletes a word they authored, so
    // an insertion that abuts the landing offset refuses. Only an insertion: a
    // deletion puts no characters in, so it cannot have written the run, and an
    // insertion with even one unchanged character between it and the span cannot
    // have reached it without being two edits instead of one.
    const abuts = inserted > 0 && head + inserted === shifted
    at = abuts ? -1 : shifted
  }
  else {
    // The walk finds ONE alignment, not the only one. The same pair of values is
    // explained by the same edit made nearer the span whenever the characters that
    // move past the boundary repeat -- dictating a phrase that repeats a word the
    // draft continues with is exactly that, and there the walk runs THROUGH the
    // span and reports an edit beyond its end while the user in fact deleted from
    // in front of it. So the boundary is slid down as far as the pair still
    // explains itself, and the span is only called intact when even the furthest
    // reading leaves it so. Monotone, because each step left exposes one more
    // character that has to match: the first mismatch is the limit.
    let earliest = head
    while (earliest > 0 && written[earliest - 1 + removed] === cur[earliest - 1 + inserted]) earliest--
    // Reaching the span's end is not the same as clearing it. An insertion that
    // begins exactly there is the far-side twin of the near-side case above: the
    // user's typing could have started one character earlier and rewritten the run's
    // own tail, so selecting a dictated `plan` and typing `planet` leaves what typing
    // `et` after it leaves, and taking the run out of the second reading deletes
    // letters they authored. A deletion is not in doubt -- it put no characters in --
    // so it still resolves at the boundary.
    const abutsFar = inserted > 0 && earliest === span.end
    if (earliest >= span.end && !abutsFar) at = span.start     // the edit is after it
  }
  if (!holds(at)) return -1
  const other = at === span.start ? shifted : span.start
  return other !== at && holds(other) ? -1 : at
}

/** Did one user edit touch the characters `[runStart, runEnd)` of `before`?
 *
 *  `locateDictationSpan` can only ask what a PAIR of values allows, and one shape
 *  defeats it: delete the dictated run and retype it byte for byte at the same
 *  offset, and the pair is identical to one nobody edited at all. Provenance is
 *  not recoverable from the result, so it has to be observed while it happens --
 *  the host reports each of its own edits as it commits it, and this answers
 *  whether that edit reached the run.
 *
 *  The region between the pair's common prefix and common suffix is where the
 *  change lies under EVERY reading of it, so no caret is needed and no alignment
 *  is chosen: a caret read a moment later is gone the first time the user clicks,
 *  and the widest region is the one that cannot miss a touch. A zero-width
 *  insertion exactly at either boundary is not a touch -- typing in front of the
 *  run or after it leaves the run's own characters untouched, and those two shapes
 *  are judged by the locator, which refuses whenever they are ambiguous. An
 *  insertion strictly inside IS a touch: those characters are no longer only the
 *  machine's.
 */
export function userEditTouchesRun(
  before: string,
  after: string,
  runStart: number,
  runEnd: number,
): boolean {
  if (before === after) return false
  let head = 0
  while (head < before.length && head < after.length && before[head] === after[head]) head++
  let tail = 0
  while (
    tail < before.length - head
    && tail < after.length - head
    && before[before.length - 1 - tail] === after[after.length - 1 - tail]
  ) tail++
  const start = head
  const end = before.length - tail
  if (end > start) return end > runStart && start < runEnd      // characters were replaced
  return start > runStart && start < runEnd                     // pure insertion, strictly inside
}
