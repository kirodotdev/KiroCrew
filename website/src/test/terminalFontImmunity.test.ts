import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

/**
 * The built-in terminal is immune to the shell's Content Text Size ladder, and
 * it is immune for one reason only: xterm receives an explicit NUMBER of pixels
 * from the terminal's own preference, never a relative unit and never the root
 * font size. This file is the fence around that reason.
 *
 * Why it matters more than a cosmetic drift. xterm lays out on a character cell
 * grid and reports its column and row count to the shell process on the other
 * end. If the glyph size changes without xterm being told, the process still
 * believes the old grid: output wraps at the wrong column and full-screen tools
 * draw into the wrong cells. The only correct repair is remeasureAndFit, and
 * Chromium's Page.setFontSizes never calls it.
 *
 * So the terminal keeps its own stepper in Settings, Display, and the ladder
 * must not reach it. A change that makes any of these assertions fail has
 * handed the terminal's glyph size to something that cannot refit it.
 */
const read = (relative: string) =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8')

const CLI_PANEL = read('../components/CliPanel.tsx')
const USE_TERMINAL_FONT = read('../hooks/useTerminalFont.ts')

// Every line that puts a font size into xterm, whichever side of the cache.
const xtermFontSizeLines = CLI_PANEL.split('\n')
  .map((line, index) => ({ line: line.trim(), number: index + 1 }))
  .filter(({ line }) => /\bfontSize\b/.test(line) && !line.startsWith('*') && !line.startsWith('//'))

describe('the terminal is immune to the shell content text size ladder', () => {
  it('takes its size from the terminal preference at construction', () => {
    // getOrCreateTerm's `new Terminal({ ... })`.
    expect(CLI_PANEL).toMatch(/fontSize: font\.fontSize/)
  })

  it('takes its size from the terminal preference on a live change', () => {
    // applyTerminalFontToAll, which is followed by remeasureAndFit so xterm
    // re-reads the cell metrics and tells the process the new grid.
    expect(CLI_PANEL).toMatch(/term\.options\.fontSize = font\.fontSize/)
  })

  it('re-measures and refits whenever it changes the live size', () => {
    const applyBlock = CLI_PANEL.slice(
      CLI_PANEL.indexOf('function applyTerminalFontToAll'),
    ).slice(0, 600)
    expect(applyBlock).toMatch(/term\.options\.fontSize = /)
    expect(applyBlock).toMatch(/remeasureAndFit\(/)
  })

  it('never hands xterm a relative unit, a CSS variable or a computed root size', () => {
    for (const { line, number } of xtermFontSizeLines) {
      expect(line, `CliPanel.tsx:${number} keeps xterm on an explicit number`)
        .not.toMatch(/\d\s*(rem|em|%)\b|var\(|getComputedStyle|documentElement/)
    }
  })

  it('keeps the preference itself a number, so no unit can arrive through it', () => {
    expect(USE_TERMINAL_FONT).toMatch(/fontSize: number/)
    // The setter clamps to a number rather than passing a value through.
    expect(USE_TERMINAL_FONT).toMatch(/typeof p\.fontSize === 'number'/)
  })

  it('pins the exact set of sites that write a size into xterm', () => {
    const assignments = xtermFontSizeLines
      .filter(({ line }) => /fontSize(:| =)/.test(line))
      .map(({ line }) => line)
      .sort()
    // Exactly two writers, both reading the terminal's own preference. A third
    // is not automatically wrong, but it must be an explicit number out of
    // getTerminalFont and it must be followed by remeasureAndFit, so it belongs
    // in this list only after someone has read the block comment above.
    expect(assignments).toEqual([
      'fontSize: font.fontSize,',
      'term.options.fontSize = font.fontSize',
    ])
  })

  it('keeps the fallback on the read sites an explicit number', () => {
    const reads = xtermFontSizeLines
      .filter(({ line }) => /term\.options\.fontSize \?\?/.test(line))
      .map(({ line }) => line)
    expect(reads.length).toBeGreaterThan(0)
    for (const read of reads) {
      // `?? 13`, never `?? someCssValue`: these two feed a cell-metric
      // calculation, so a non-number here miscounts columns.
      expect(read).toMatch(/term\.options\.fontSize \?\? \d+/)
    }
  })
})
