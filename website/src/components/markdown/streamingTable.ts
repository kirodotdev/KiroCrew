/**
 * How a table at the tail of a streaming markdown block is shown while its
 * text is still arriving. Both functions take the block's text and return the
 * part of it to draw now; neither applies once the stream has ended.
 */

// A GFM table delimiter row, e.g. `| --- | :--: |` or `---|---`. remark-gfm
// only promotes the preceding header line to a <table> once this row is present.
const TABLE_DELIM_RE = /^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/
// A bordered table line: the header and rows a model writes (`| a | b |`).
const BORDERED_ROW_RE = /^\s*\|/
// A real GFM delimiter row carries at least one pipe; a bare `---` does not.
const isDelimiterRow = (line: string): boolean => line.includes('|') && TABLE_DELIM_RE.test(line)

/**
 * While STREAMING, withhold an incomplete trailing table so it never paints as
 * literal pipe text that later reflows into a <table>.
 *
 * remark-gfm needs BOTH a header row and a `|---|` delimiter row to recognize a
 * table. Mid-stream the header arrives first and renders as a <p> containing
 * literal "| A | B |"; when the delimiter row streams in, that paragraph
 * RESTRUCTURES into a bordered table — a visible structural snap of
 * already-shown content (see MarkdownRenderer.streamingTableSnap.test.tsx).
 * This defers the trailing header run (mirroring how an incomplete fenced code
 * block is held) until the delimiter arrives, so the transition the user sees
 * is the standard "content appears", not "paragraph morphs into a table".
 *
 * Scoped narrowly to avoid hiding ordinary prose: only a run of trailing
 * non-blank lines whose FIRST line is a bordered table header (starts with `|`)
 * is a candidate, and only when that run does NOT yet contain a delimiter row
 * (a `---` row that actually carries a `|`). A run that already has such a
 * delimiter is a real (possibly still growing) table and is left to render.
 *
 * Scoping choices (both close known edge cases):
 *  - Require the first line to START with `|`. A looser "≥2 pipes" test also
 *    matched ordinary prose (e.g. a line with an inline `` `cmd | grep | wc` ``)
 *    and would withhold that whole paragraph for the rest of the stream. Models
 *    emit bordered tables (`| a | b |`), so start-with-`|` keeps the real case
 *    while excluding prose; a borderless table simply isn't deferred (it never
 *    regressed anything — it just renders as before).
 *  - The delimiter must contain a `|`. A bare `---` is a thematic break / setext
 *    underline, NOT a GFM table delimiter (which needs matching pipe-separated
 *    cells), so counting it as "already a table" would wrongly skip deferral and
 *    let the snap happen.
 */
export function deferIncompleteStreamingTable(content: string): string {
  const lines = content.split('\n')
  let start = lines.length
  while (start > 0 && lines[start - 1].trim() !== '') start--
  if (start >= lines.length) return content // trailing blank line / nothing to defer
  const run = lines.slice(start)
  if (!BORDERED_ROW_RE.test(run[0])) return content // not a bordered table header
  if (run.some(isDelimiterRow)) return content
  return lines.slice(0, start).join('\n')
}

/**
 * While STREAMING, hold back the half-typed last row of a trailing table, so
 * the table grows one whole row at a time.
 *
 * A table is one markdown node: every character added to its last row changes
 * the text of the whole table, so the parser re-reads all of it and the browser
 * restyles every cell, once per animation frame. Holding the row until its
 * line break arrives leaves the text unchanged in between, and a block whose
 * text is unchanged is not parsed again (see ParsedMarkdown). The cost of a
 * table then follows its row count, not its character count.
 *
 * Only a line that follows a table already on screen is held: the text ends
 * without a line break, its last line starts with `|`, and the run of non-blank
 * lines above it opens with a bordered header and has its delimiter row. The
 * header and the delimiter row are `deferIncompleteStreamingTable`'s, and a
 * line under anything else (prose, a list, a table with no delimiter yet) is
 * returned untouched. The held text keeps its final line break, so it is the
 * same string the block had when the previous row completed.
 */
export function holdPartialTableRow(content: string): string {
  const nl = content.lastIndexOf('\n')
  // One line only, or the text ends on a line break: no half-typed line.
  if (nl < 0 || nl === content.length - 1) return content
  if (!BORDERED_ROW_RE.test(content.slice(nl + 1))) return content
  const above = content.slice(0, nl).split('\n')
  let start = above.length
  while (start > 0 && above[start - 1].trim() !== '') start--
  const run = above.slice(start)
  if (run.length < 2 || !BORDERED_ROW_RE.test(run[0])) return content
  if (!run.slice(1).some(isDelimiterRow)) return content
  return content.slice(0, nl + 1)
}
