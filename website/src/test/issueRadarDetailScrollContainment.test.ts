/**
 * Every scroller in the issue and pull detail panes is a containing block.
 *
 * A pane that scrolls with `overflow-y-auto` clips only the absolutely
 * positioned descendants whose containing block is the pane itself or lies
 * inside it. Markdown copy chips carry an always-mounted `sr-only` status
 * region (`position: absolute`), so in an unpositioned scroller each one is
 * placed against the page wrapper instead, at its static position far down a
 * long summary. Those regions then stretch the page past the viewport, the
 * shell's `<main>` gains something to scroll, and the whole Issue Radar layout
 * slides up under the header.
 *
 * Asserted over source: jsdom performs no layout, so a render cannot measure
 * the overflow this guards against.
 */
import { describe, it, expect } from 'vitest'

async function src(path: string): Promise<string> {
  return (await import(`../apps/issue-radar/${path}?raw`)).default as string
}

const SCROLLERS = [
  // Narrow: the wrapper is the single scroller.
  /className="([^"]*\bh-full flex flex-col overflow-y-auto sm:overflow-visible[^"]*)"/,
  // Wide: the description/timeline column scrolls on its own.
  /<main className="([^"]*\bsm:overflow-y-auto[^"]*)"/,
  // Wide: the metadata sidebar scrolls on its own.
  /<aside className="([^"]*\bsm:overflow-y-auto[^"]*)"/,
]

describe('Issue Radar detail panes contain their absolutely positioned content', () => {
  for (const pane of ['IssueDetail', 'PrDetail']) {
    it(`${pane}: every scroller is positioned`, async () => {
      const s = await src(`components/${pane}.tsx`)
      for (const re of SCROLLERS) {
        const m = s.match(re)
        expect(m, `${pane}: expected a scroller matching ${re}`).not.toBeNull()
        expect(m![1].split(/\s+/), `${pane}: "${m![1]}" must be a containing block`)
          .toContain('relative')
      }
    })
  }
})
