/// <reference lib="webworker" />
// Diff-compute worker — runs jsdiff's patch creation off the main thread.
//
// Why this worker exists: `MultiFileDiff` builds its raw diff SYNCHRONOUSLY on
// the renderer thread before Pierre's highlight workers or row virtualization
// can help (see `config.ts` — 400 fully-changed lines ≈ 20 ms, 1,000 ≈ 120 ms,
// superlinear). For the oversized-pair opt-in ("Show line-by-line diff") that
// cost is unbounded by the render budget, so it moves here: the UI thread posts
// the two file bodies, this worker computes a unified patch, and the main
// thread renders the RESULT through the hunk-based patch path — DOM stays
// proportional to changed lines + context, never to file size. This mirrors
// how large code-review UIs work (diff computed off the UI thread, hunks
// loaded incrementally), with a Web Worker standing in for their server.
//
// jsdiff is `@pierre/diffs`' own diff engine, so the hunks computed here are
// byte-identical in shape to what `MultiFileDiff` would have produced.
import { createTwoFilesPatch } from 'diff'

export interface PairDiffRequest {
  id: number
  oldName: string
  newName: string
  oldContents: string
  newContents: string
}

export type PairDiffResponse =
  | { id: number; ok: true; patch: string }
  | { id: number; ok: false; error: string }

/** Pure request handler — exported so its behaviour is unit-testable outside a
 *  worker context (jsdom/node have no `Worker`; the message plumbing below is
 *  the only part that needs one). */
export function handlePairDiffRequest({ id, oldName, newName, oldContents, newContents }: PairDiffRequest): PairDiffResponse {
  try {
    // Patch headers are line-oriented: a CR/LF inside a filename would break
    // the `--- name` line and let a crafted name inject forged hunk rows into
    // the parsed patch. Names are display-only here, so flatten line breaks.
    const safeOld = oldName.replace(/[\r\n]+/g, ' ')
    const safeNew = newName.replace(/[\r\n]+/g, ' ')
    // `context: 3` matches git's default hunk context. Identical names render
    // as a plain file header; distinct names as a rename — both are what the
    // parsed-patch renderer expects.
    const patch = createTwoFilesPatch(safeOld, safeNew, oldContents, newContents, undefined, undefined, { context: 3 })
    return { id, ok: true, patch }
  } catch (err) {
    return { id, ok: false, error: err instanceof Error ? err.message : String(err) }
  }
}

/** Wires the message plumbing onto a worker scope. Exported so the wiring is
 *  unit-testable with a fake scope (jsdom/node have no `Worker`). */
export function attachPairDiffHandler(
  ctx: { onmessage: ((e: MessageEvent<PairDiffRequest>) => void) | null; postMessage: (r: PairDiffResponse) => void },
): void {
  ctx.onmessage = (e: MessageEvent<PairDiffRequest>) => {
    ctx.postMessage(handlePairDiffRequest(e.data))
  }
}

// Attach only inside a real worker scope: the pure handler above is importable
// from tests (jsdom window, node) without touching globals.
if (typeof WorkerGlobalScope !== 'undefined' && typeof self !== 'undefined') {
  attachPairDiffHandler(self as unknown as DedicatedWorkerGlobalScope)
}
