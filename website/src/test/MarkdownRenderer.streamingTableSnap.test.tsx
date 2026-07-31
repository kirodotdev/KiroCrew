import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * REGRESSION GUARD (was a failing repro) — gap #2: TABLES.
 *
 * Tables are NOT a first-class block in `useBlockAssembler` (only code / diff /
 * mermaid / widget are); they render inline inside the markdown block, so they
 * get no `SmoothResize` wrapper and no streaming-aware handling. remark-gfm only
 * recognizes a GFM table once BOTH the header row and the `|---|---|` delimiter
 * row are present. So mid-stream:
 *
 *   1. the header line arrives  → renders as a <p> with literal "| A | B |" text
 *   2. the delimiter line arrives → the subtree RESTRUCTURES into a bordered
 *      <table> (padding, borders, overflow-x wrapper, different margins)
 *
 * Step 2 is a structural reflow of already-visible content — a snap/flash that
 * neither the `.ft-word` opacity fix (#661/#697, text-edge only) nor #824
 * (virtualizer spacer only) addresses. The desired behavior while streaming is
 * that an incomplete trailing table region is NOT shown as raw pipe-delimited
 * paragraph text that will later restructure — either withhold it until the
 * delimiter arrives (mirroring how incomplete fences are held) or render it as a
 * provisional table. Both fixes share one observable property: the literal
 * "| Col A | Col B |" pipe row is never painted as visible paragraph text.
 *
 * The GAP test asserts that property and FAILS on current `main`. The PREMISE
 * test documents the current paragraph→table snap and stays true after the fix.
 */

const STREAM = { streaming: true, glow: true, smooth: true } as const

/** Collapsed visible text of the render (whitespace-normalized). */
function visibleText(container: HTMLElement): string {
  return (container.textContent || '').replace(/\s+/g, ' ').trim()
}

describe('streaming table structural-snap regression (gap #2)', () => {
  it('PREMISE: without a delimiter row there is no <table> yet (table needs header + |---|)', () => {
    // Survives the fix — establishes that a lone header line is genuinely
    // pre-table, so the GAP below is about HOW that pre-table state is shown.
    const { container } = render(
      <MarkdownRenderer content={'| Col A | Col B |'} {...STREAM} />,
    )
    expect(container.querySelector('table')).toBeNull()
  })

  it('PREMISE: once the delimiter row arrives the same content becomes a <table>', () => {
    // Survives the fix — documents that a real structural transition happens
    // (paragraph text → table), which is exactly the reflow we want to hide.
    const { container } = render(
      <MarkdownRenderer content={'| Col A | Col B |\n| --- | --- |\n| 1 | 2 |'} {...STREAM} />,
    )
    expect(container.querySelector('table')).not.toBeNull()
  })

  it('GAP: while streaming, an incomplete table header is not painted as literal pipe text', () => {
    // The realistic streaming sequence: the header (and maybe a first data line)
    // has arrived but the delimiter has not yet. Today this renders as a <p>
    // containing "| Col A | Col B |", which then reflows into a bordered table
    // the moment the delimiter streams in — the visible snap.
    const { container } = render(
      <MarkdownRenderer content={'| Col A | Col B |'} {...STREAM} />,
    )
    // Regression guard: while streaming, the incomplete header run is withheld
    // (deferIncompleteStreamingTable) rather than painted as a <p> of literal
    // pipes that would later reflow into a bordered table.
    expect(visibleText(container)).not.toContain('| Col A | Col B |')
  })

  it('GAP EDGE: a header followed only by a bare `---` (thematic break, not a real delimiter) is still deferred', () => {
    // `| A | B |` + `---` is NOT a GFM table (a delimiter row needs pipe-separated
    // cells), so the header would still snap into a table only once a real
    // `| --- | --- |` arrives. The delimiter check requires a `|`, so the bare
    // `---` does not count and the run stays deferred.
    const { container } = render(
      <MarkdownRenderer content={'| Col A | Col B |\n---'} {...STREAM} />,
    )
    expect(visibleText(container)).not.toContain('| Col A | Col B |')
  })

  it('SCOPING: ordinary streaming prose containing inline pipes is NOT withheld', () => {
    // Guards against over-broad deferral: a trailing prose line with pipes (e.g.
    // an inline shell pipeline) must keep rendering — only a line that STARTS
    // with `|` (a bordered table header) is treated as a deferrable table start.
    const { container } = render(
      <MarkdownRenderer content={'Run `cmd | grep x | wc` to count.'} {...STREAM} />,
    )
    expect(visibleText(container)).toContain('Run')
    expect(container.querySelector('table')).toBeNull()
  })
})
