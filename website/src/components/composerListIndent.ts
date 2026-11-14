/**
 * Tab / Shift+Tab indentation for list lines in the plain-text composer.
 *
 * Pure text math: the textarea composer decides WHEN to ask and applies the
 * returned value and selection. The stored prompt stays plain text: an indent is
 * literal spaces after the line's own leading whitespace, two per level or the
 * parent item's text column under an ordered marker.
 *
 * A numbered item that changes level is counted at the level it lands on.
 * CommonMark starts a list at its first item's number, so a `2.` moved under
 * `1.` and left as typed opens a nested list that begins at 2 (see `Run`).
 */

import {
  indentShift,
  isListLine,
  listContentColumn,
  nextOrdinal,
  parseMarker,
  renumberFollowing,
  type ListMarker,
} from './composerListContinuation'

/** One indent level. */
const LIST_INDENT = '  '

/** The new draft and the selection to restore; `caret === end` when collapsed. */
export interface ListIndentEdit {
  value: string
  caret: number
  end: number
}

/** `removed` characters at `at` replaced by `insert`. */
interface Splice {
  at: number
  removed: number
  insert: string
  /** A number rewrite, or the move of a line nested under a renumbered item: a caret at its start stays in front of it. */
  rewrite?: true
}

/**
 * The numbered items one key press moves to another level together: the first
 * selected one and the selected siblings that count on from it.
 *
 * The first takes the number its new level gives it: the one after the numbered
 * item right above it there, or 1 when it opens a list. Each selected sibling the
 * user had counted in sequence counts on from it. The unselected items below
 * follow by `renumberFollowing`'s rule, which moves only a run the user numbered
 * in sequence:
 *
 * - Tab: the old level's items below take the numbers the run gave up.
 * - Shift+Tab: the old level's items below sit under the run's last item, and
 *   restart at 1 when they reach its text column; the new level's items below
 *   count on after the run.
 */
interface Run {
  /** The number the first item had. */
  firstTyped: string
  /** The last item so far: its marker as typed, its new number, its line's end. */
  last: ListMarker
  lastNew: string
  lastEnd: number
  /** The indent width the run lands on. */
  landing: number
  /**
   * Shift+Tab only: the parent-level item above the run as it was typed, when
   * the run counts on at that level. The items below the run are matched against it.
   */
  upper: ListMarker | null
}

function lineBounds(value: string, offset: number): { start: number; end: number } {
  const start = offset === 0 ? 0 : value.lastIndexOf('\n', offset - 1) + 1
  const newline = value.indexOf('\n', offset)
  return { start, end: newline === -1 ? value.length : newline }
}

/** Count of leading spaces and tabs. */
function leadingWidth(line: string): number {
  return line.length - line.replace(/^[ \t]+/, '').length
}

/**
 * The closest list line above `lineStart` indented at most `maxIndent`, skipping
 * other lines, or `null`. A negative `maxIndent` finds nothing.
 */
function nearestListLineAbove(value: string, lineStart: number, maxIndent: number): string | null {
  if (maxIndent < 0) return null
  // Line by line upwards, stopping at the first match: a selection asks once per line.
  for (let end = lineStart - 1; end > 0;) {
    const start = value.lastIndexOf('\n', end - 1) + 1
    const line = value.slice(start, end)
    if (isListLine(line) && leadingWidth(line) <= maxIndent) return line
    end = start - 1
  }
  return null
}

/**
 * Where `offset` lands once `splice` is applied: inside the removed text it lands
 * at `at`, and at the start of a number rewrite it stays in front of the number.
 */
function shift(offset: number, splice: Splice): number {
  if (offset < splice.at || (splice.rewrite && offset === splice.at)) return offset
  return Math.max(splice.at, offset - splice.removed) + splice.insert.length
}

/**
 * What Tab (`outdent` false) or Shift+Tab (`outdent` true) does to the list line
 * that starts at `lineStart` with `indent` leading spaces and tabs, or `null` for
 * an outdent on a line that has no leading indentation.
 */
function lineSplice(value: string, lineStart: number, indent: number, outdent: boolean): Splice | null {
  const above = nearestListLineAbove(value, lineStart, outdent ? indent - 1 : indent)

  if (!outdent) {
    // Nest under the sibling above at its text column (`1. ` needs 3 spaces to
    // nest under CommonMark, `- ` needs 2); otherwise one two-space level. The
    // pad goes after the line's own indentation, so a leading tab keeps its stop.
    const column = above && leadingWidth(above) === indent ? listContentColumn(above) : null
    const pad = column !== null && column > indent ? ' '.repeat(column - indent) : LIST_INDENT
    return { at: lineStart + indent, removed: 0, insert: pad }
  }

  // Remove one level next to the marker, as Tab added it: whatever lies past the
  // parent's indent when there is a parent above, else a trailing tab or up to
  // two trailing spaces.
  if (above) {
    const keep = leadingWidth(above)
    return { at: lineStart + keep, removed: indent - keep, insert: '' }
  }
  if (indent === 0) return null
  const removed = value.endsWith(LIST_INDENT, lineStart + indent) ? 2 : 1
  return { at: lineStart + indent - removed, removed, insert: '' }
}

/**
 * `lead`'s step taken by a list line beside or under it: Tab adds the same pad
 * after the line's own indentation, Shift+Tab removes the same text from the end
 * of it, or `null` when the indentation does not end with that text.
 */
function followSplice(value: string, lead: Splice, lineStart: number, indent: number): Splice | null {
  const at = lineStart + indent
  if (lead.insert) return { at, removed: 0, insert: lead.insert }
  const cut = value.slice(lead.at, lead.at + lead.removed)
  return value.endsWith(cut, at) ? { at: at - cut.length, removed: cut.length, insert: '' } : null
}

/**
 * A selected child's `step` taken `columns` further, so the child keeps its
 * distance from a parent whose number gained or lost digits and still starts at
 * or past the parent's text column: more spaces after the step, a shorter pad,
 * or more of the indentation cut.
 */
function widened(value: string, step: Splice, columns: number): Splice {
  // A number that lost digits first takes back the pad the step adds, then spaces of the indentation.
  const pad = columns < 0 ? step.insert.slice(0, Math.max(0, step.insert.length + columns)) : step.insert
  const move = indentShift(value, step.at, columns + step.insert.length - pad.length)
  return { at: move.start, removed: step.removed + move.end - move.start, insert: pad + move.insert }
}

/** 1 in the zero padding of `digits`: `07` gives `01`. */
function firstOrdinal(digits: string): string {
  return digits.length > 1 && digits.startsWith('0') ? '1'.padStart(digits.length, '0') : '1'
}

/** `text` with the `splices` that start inside it applied. */
function applied(text: string, splices: readonly Splice[]): string {
  let next = ''
  let from = 0
  for (const splice of [...splices].sort((a, b) => a.at - b.at)) {
    if (splice.at >= text.length) break
    next += text.slice(from, splice.at) + splice.insert
    from = splice.at + splice.removed
  }
  return next + text.slice(from)
}

/**
 * The run a numbered line opens when `step` moves it to another level, or `null`
 * when it keeps its number: a bullet, or an item that stays the first of its own
 * list because Tab found no sibling above to nest under or Shift+Tab no parent
 * level to land on. `before` is the draft above the line as this key press has
 * edited it so far, where the number to count on from is read: a line above may
 * have moved or been renumbered by an earlier line of the same selection.
 */
function openRun(
  value: string,
  line: { start: number; end: number },
  indent: number,
  step: Splice,
  outdent: boolean,
  before: string,
): Run | null {
  const marker = parseMarker(value.slice(line.start, line.end))
  if (!marker?.digits) return null
  const anchor = nearestListLineAbove(value, line.start, outdent ? indent - 1 : indent)
  if (anchor === null || (!outdent && leadingWidth(anchor) !== indent)) return null
  // The item the run lands behind: on Tab the last one already at the landing
  // column under the sibling, on Shift+Tab the parent-level item above.
  const landing = outdent ? step.at - line.start : indent + step.insert.length
  const above = nearestListLineAbove(before, before.length, outdent ? indent - 1 : landing)
  const neighbour = above !== null && leadingWidth(above) === landing ? parseMarker(above) : null
  const upper = neighbour?.digits && neighbour.delimiter === marker.delimiter ? neighbour : null
  // The items below are still as typed, so they are matched against the number
  // the parent-level item above had before this key press.
  const typed = parseMarker(anchor)
  return {
    firstTyped: marker.digits,
    last: marker,
    lastNew: upper ? nextOrdinal(upper.digits!) : firstOrdinal(marker.digits),
    lastEnd: line.end,
    landing,
    upper: outdent && upper && typed?.digits && typed.delimiter === marker.delimiter ? typed : null,
  }
}

/** Whether the item with `marker` counts on from `run`'s last item as typed. */
function continues(run: Run, marker: ListMarker | null): marker is ListMarker {
  return !!marker?.digits && marker.delimiter === run.last.delimiter && marker.digits === nextOrdinal(run.last.digits!)
}

/**
 * The rewrites `run` leaves on the unselected lines below it, read in `value`
 * up to `limit`: the numbers of the items, and the indentation of the lines
 * nested under an item whose number changed width. The next selected line that
 * is not inside the run takes its own step, so nothing from there on follows
 * this run.
 */
function followers(value: string, limit: number, run: Run, outdent: boolean): Splice[] {
  const scope = value.slice(0, limit)
  const below = (marker: ListMarker, first: string) => renumberFollowing(scope, run.lastEnd, marker, [], first)
  const nested = run.last.indent.length >= run.landing + run.lastNew.length + 1 + run.last.gap.length
  const writes = outdent
    ? [
        ...(nested ? below(run.last, firstOrdinal(run.last.digits!)) : []),
        ...(run.upper ? below(run.upper, nextOrdinal(run.lastNew)) : []),
      ]
    : below(run.last, run.firstTyped)
  return writes.map((write): Splice => ({ at: write.start, removed: write.end - write.start, insert: write.insert, rewrite: true }))
}

/**
 * The edit Tab (`outdent` false) or Shift+Tab (`outdent` true) should make on the
 * caret's line, or on every list line a selection over several lines touches, or
 * `null` when the key is not ours and must keep its default meaning (moving
 * focus): a selection inside one line, no list line to change, or an outdent
 * where no list line has leading indentation. A selection that ends at the start
 * of a line leaves that line alone.
 */
export function listIndentEdit(
  value: string,
  selectionStart: number,
  selectionEnd: number,
  outdent: boolean,
): ListIndentEdit | null {
  let caret = Math.max(0, Math.min(selectionStart, value.length))
  let end = Math.min(selectionEnd, value.length)
  if (caret !== end && !value.slice(caret, end).includes('\n')) return null
  const starts = [lineBounds(value, caret).start]
  for (let nl = value.indexOf('\n', starts[0]); nl !== -1 && nl < end - 1; nl = value.indexOf('\n', nl + 1)) {
    starts.push(nl + 1)
  }

  // The first selected list line at each depth takes its own step, measured on
  // the draft as it was; its selected siblings and children take the same step
  // where they can, so the selection moves as one block. `grow` is how many
  // digits an item's number gained, which its selected children step further by.
  const splices: Splice[] = []
  const items: { indent: number; splice: Splice | null; grow: number }[] = []
  let run: Run | null = null
  const closeRun = (limit: number) => {
    if (run) splices.push(...followers(value, limit, run, outdent))
    run = null
  }
  for (const lineStart of starts) {
    const line = lineBounds(value, lineStart)
    const text = value.slice(line.start, line.end)
    if (!isListLine(text)) continue
    const indent = leadingWidth(text)
    while (items.length > 0 && items[items.length - 1].indent > indent) items.pop()
    const sibling = items[items.length - 1]?.indent === indent ? items.pop() : undefined
    const parent = items[items.length - 1]
    const lead = sibling ?? parent
    const followed = lead?.splice ? followSplice(value, lead.splice, lineStart, indent) : null
    const splice = followed ?? lineSplice(value, lineStart, indent, outdent)
    if (!splice) {
      // Shift+Tab cannot move a line with no indentation. It stays where it is and
      // is counted like an unselected line: it ends no run and leads no child.
      items.push({ indent, splice, grow: 0 })
      continue
    }
    splices.push(followed && parent ? widened(value, followed, parent.grow) : splice)
    let grow = followed && parent ? parent.grow : 0
    if (!parent?.splice) {
      // Not under a selected item that moves, so its level changes with this key press.
      const marker = parseMarker(text)
      if (!sibling) {
        closeRun(lineStart)
        run = openRun(value, line, indent, splice, outdent, applied(value.slice(0, lineStart), splices))
      } else if (run && followed && continues(run, marker)) {
        run.last = marker
        run.lastNew = nextOrdinal(run.lastNew)
        run.lastEnd = line.end
      } else {
        // Off the count: its number and those below it are the user's own.
        run = null
      }
      if (run && marker?.digits) {
        // This line opened the run or counts on in it.
        const typed = marker.digits
        if (run.lastNew !== typed) {
          splices.push({ at: lineStart + indent, removed: typed.length, insert: run.lastNew, rewrite: true })
        }
        grow = run.lastNew.length - typed.length
      }
    }
    items.push({ indent, splice, grow })
  }
  closeRun(value.length)
  if (splices.length === 0) return null
  // Document order. A stable sort keeps a line's pad in front of its number
  // rewrite, which starts at the same offset.
  splices.sort((a, b) => a.at - b.at)
  const next = applied(value, splices)
  // Last splice first: each one's `at` is an offset in the original draft, which
  // only the splices below it would have moved.
  for (const splice of splices.reverse()) {
    caret = shift(caret, splice)
    end = shift(end, splice)
  }
  return { value: next, caret, end }
}

/** True when any line of `value` is a list line, so Tab may indent somewhere. */
export function hasListLine(value: string): boolean {
  return value.split('\n').some(isListLine)
}
