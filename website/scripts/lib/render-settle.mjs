/**
 * When a captured surface is DONE rendering.
 *
 * The gate used to answer that with a fixed sleep: paint the shell, sleep
 * `surface.settle` milliseconds, scan. Every panel that fetches AFTER mount was
 * then a race, and losing it was silent -- the page still rendered, just its
 * loading skeleton, so the surface reported a SMALLER number instead of failing.
 *
 * `[vs-base]` subtracts two of those captures, so one lost race on the base sweep
 * is reported as findings the branch ADDED. That is how a documentation-only pull
 * request drew `app-detail.text: 44 -> 70 (+26)`: the head sweep measured the
 * populated detail body (70, which is also the number `render-baseline.json`
 * records), the base sweep measured the skeleton (44), and the difference was
 * charged to a diff with no rendering changes in it.
 *
 * So the capture waits for two conditions instead of a clock, and both have to
 * hold at the same time before a sample counts as quiet:
 *
 *   - NOTHING IN FLIGHT. A post-mount fetch that has not answered yet is exactly
 *     the skeleton case, and no amount of DOM stillness distinguishes it from a
 *     finished page.
 *   - THE DOM HOLDS STILL. The response has to be rendered, not merely received.
 *     Measured as text volume plus node count, which is locale-agnostic on
 *     purpose: the same predicate has to work under the pseudolocale and under
 *     zh-CN, so it cannot look for any particular string.
 *
 * The tracker is a pure state machine over samples for the reason
 * `renderVerdict.test.ts` gives for its own subject: reaching this logic through
 * two vite builds and a browser is a ten-minute round trip, which is what kept the
 * old behaviour untested while it mis-blamed pull requests.
 *
 * The per-surface `settle` value is NOT gone. It survives in the caller as a
 * minimum elapsed time, because a page that has not dispatched its fetch yet looks
 * exactly like one that never will. It runs alongside the quiet window instead of
 * before it, so a surface that was already settled during its old sleep is scanned
 * at the same moment it always was.
 */

/** Milliseconds between samples. */
export const SETTLE_POLL_MS = 100

/**
 * Consecutive QUIET samples required. Three matches spans ~300ms of no network and
 * no mutation, which is longer than the gap between a stubbed response and the React
 * commit that renders it. Measured cost of the whole change at this setting: a full
 * sweep of 114 captures (57 surfaces x 2 viewports) went from 113s to 158s, with an
 * identical census, so a `[vs-base]` job pays it twice.
 */
export const SETTLE_STABLE_SAMPLES = 3

/**
 * Cap on the whole wait. Reaching it means the surface never went quiet, which the
 * caller must report rather than absorb: a page that mutates forever cannot be
 * measured reproducibly, and scanning it anyway is the silent understatement this
 * module exists to remove.
 */
export const SETTLE_TIMEOUT_MS = 10000

/**
 * True when two samples describe the same rendered page.
 *
 * Node count is carried alongside text length because a swap can preserve length
 * -- a skeleton row replaced by a real row of the same width is a different page
 * with the same character count.
 */
export function sameRender(a, b) {
  if (!a || !b) return false
  return a.chars === b.chars && a.nodes === b.nodes
}

/**
 * A sample is quiet when the page owes nothing to the network. `inflight` is a
 * started-minus-settled difference, so a straggler from the previously captured
 * surface can drive it negative; treat that as idle rather than as a request that
 * has yet to be made.
 */
export function sampleIsQuiet(sample) {
  return Boolean(sample) && sample.inflight <= 0
}

/**
 * Accumulates samples until the surface has held still long enough.
 *
 * `observe` returns true the moment the run is settled, and never returns to false
 * afterwards for the same tracker -- one tracker per capture, meaning one per
 * surface per viewport per locale.
 */
export function createSettleTracker({ stableSamples = SETTLE_STABLE_SAMPLES } = {}) {
  let previous = null
  let stable = 0
  let settled = false

  return {
    observe(sample) {
      if (settled) return true
      // A sample only extends the run when the page is idle AND unchanged. Either
      // one alone is the failure mode: idle-but-changing is React still
      // committing, unchanged-but-busy is the skeleton waiting on its fetch.
      stable = sampleIsQuiet(sample) && sameRender(previous, sample) ? stable + 1 : 0
      previous = sample
      settled = stable >= stableSamples
      return settled
    },
    get settled() { return settled },
    get stable() { return stable },
    get last() { return previous },
  }
}
