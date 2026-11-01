/**
 * The bridge between a table grid and the ask box (PoC). A table is one widget of
 * inputs, so text selection never reaches its rows; its row handles ask through
 * these effects instead. Kept apart from `docConductor` and `docTables` so each
 * can import it without importing the other.
 */
import { StateEffect, StateField } from '@codemirror/state'

/** Ask about some of a table's rows: the box opens under [from, to] (the whole
 *  table, where the markers go) and the conductor is sent *quote*. */
export interface RowAsk { from: number; to: number; quote: string; rows: number[] }

export const requestRowAsk = StateEffect.define<RowAsk>()
/** The ask box was sent or put away. */
export const closeDraft = StateEffect.define<null>()

/** The rows picked for the open ask, so the grid can tint them. */
export const askRows = StateField.define<{ from: number; rows: number[] } | null>({
  create: () => null,
  update(v, tr) {
    let next = v
    if (tr.docChanged || tr.selection) next = null
    for (const e of tr.effects) {
      if (e.is(requestRowAsk)) next = { from: e.value.from, rows: e.value.rows }
      else if (e.is(closeDraft)) next = null
    }
    return next
  },
})

/** A label for a row ask: the first picked row's cells, from a quote that is a
 *  small markdown table (header, separator, rows). Null for any other quote. */
export function rowQuoteLabel(quote: string): string | null {
  const lines = quote.split('\n').map(l => l.trim())
  if (lines.length < 3 || !lines.every(l => l.startsWith('|'))) return null
  const row = lines.slice(2).find(l => !/^\|[\s:|-]+\|?$/.test(l))
  if (!row) return null
  const cells = row.replace(/^\||\|$/g, '').split('|').map(c => c.trim()).filter(Boolean)
  const more = lines.length - 3
  return cells.join(' · ') + (more > 0 ? ` (+${more})` : '')
}
