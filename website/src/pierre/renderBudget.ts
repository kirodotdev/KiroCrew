import type { FileContents } from '@pierre/diffs'
import {
  PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE,
  PIERRE_FILE_PAIR_MAX_TOTAL_CODE_UNITS,
} from './config'

/** Count only until the answer is known, so rejecting a huge file stays bounded. */
function hasAtMostLines(contents: string, limit: number): boolean {
  if (contents.length === 0) return true
  let lines = 1
  for (let i = 0; i < contents.length; i++) {
    if (contents.charCodeAt(i) === 10 && ++lines > limit) return false
  }
  return true
}

/**
 * Pierre computes an old/new diff synchronously before its worker pool or
 * virtualizer participates. Keep that renderer-thread work inside a measured
 * budget; callers outside it preserve both full files through a plain surface.
 */
export function isPierreFilePairWithinBudget(
  oldFile: FileContents | null,
  newFile: FileContents | null,
): boolean {
  const oldContents = oldFile?.contents ?? ''
  const newContents = newFile?.contents ?? ''
  if (oldContents.length + newContents.length > PIERRE_FILE_PAIR_MAX_TOTAL_CODE_UNITS) return false
  return hasAtMostLines(oldContents, PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE)
    && hasAtMostLines(newContents, PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE)
}
