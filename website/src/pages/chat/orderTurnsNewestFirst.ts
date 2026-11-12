import { isSynthesisInjection, opensDisplayTurn } from './groupDisplayItems'
import type { ChatMessage } from '../../types'
import type { DisplayItem } from './types'

export interface NewestFirstRows<T> {
  /** The same row objects, newest turn block first. Rows inside a block keep
   *  their chronological order, so a turn still reads prompt, then reply. */
  rows: T[]
  /** Index in `rows` of the LAST row of the newest block: where the live reply
   *  streams in the top-down layout, the role `rows.length - 1` plays in the
   *  chronological one. -1 for an empty list. */
  headBlockEnd: number
  /** `chrono[i]` is the chronological index of `rows[i]`. Row keys read it
   *  (`uniqueRowKeys`): their position rules (the headless oldest turn, the
   *  trailing turn, the duplicate tie-break) are chronological. */
  chrono: number[]
}

/** The message a display row leads with, for the block-boundary test. */
const leadMessage = (it: DisplayItem): ChatMessage | undefined => {
  if (it.kind === 'single') return it.msg
  if (it.kind === 'group') return it.msgs[0]
  const first = it.items[0]
  if (!first) return undefined
  return first.kind === 'single' ? first.msg : first.msgs[0]
}

/**
 * Does this display row open a turn block? Its lead message must open a turn
 * by `opensDisplayTurn`, the same test the grouper splits turns on (an opener
 * role, or an inject whose kind dispatched a turn of its own). An `interim`
 * fold never opens one even when it starts with an opener: it holds the
 * sub-agent completions of the fan-out it folds and belongs to the block of
 * the prompt that started that fan-out. The synthesis inject never opens one
 * either: the grouper takes it before its opener test, and the answer it leads
 * answers that same prompt.
 */
export const opensTurnBlock = (it: DisplayItem): boolean => {
  if (it.kind === 'turn' && it.interim) return false
  const lead = leadMessage(it)
  return lead !== undefined && opensDisplayTurn(lead) && !isSynthesisInjection(lead)
}

/**
 * Reorder transcript rows for the top-down layout: split them into turn blocks
 * (an opener row plus every row after it up to the next opener) and emit the
 * blocks newest first. Rows before the first opener (a compaction card or a
 * leading reply whose prompt is on an unloaded page) form the oldest block.
 *
 * Pure, O(n), and returns the input row objects unchanged so keyed memo and
 * height caches survive the reorder. `isOpener` lets a caller with its own row
 * shape reuse the block rule.
 */
export function orderTurnsNewestFirst<T = DisplayItem>(
  items: readonly T[],
  isOpener: (it: T) => boolean = opensTurnBlock as unknown as (it: T) => boolean,
): NewestFirstRows<T> {
  if (items.length === 0) return { rows: [], headBlockEnd: -1, chrono: [] }
  // Block start indices, chronological. Index 0 always starts a block, whether
  // or not it is an opener.
  const starts: number[] = [0]
  for (let i = 1; i < items.length; i++) if (isOpener(items[i])) starts.push(i)
  const rows: T[] = new Array(items.length)
  const chrono: number[] = new Array(items.length)
  let w = 0
  for (let b = starts.length - 1; b >= 0; b--) {
    const end = b + 1 < starts.length ? starts[b + 1] : items.length
    for (let i = starts[b]; i < end; i++) {
      rows[w] = items[i]
      chrono[w++] = i
    }
  }
  const headBlockEnd = items.length - starts[starts.length - 1] - 1
  return { rows, headBlockEnd, chrono }
}
