/**
 * Issue #14557 — a row of the "N files changed" card reads, left to right:
 * chevron · file icon · filename · [artifact badge + diffstat cells] · `-N +N`.
 *
 * The filename is the ONE item built to absorb a squeeze (`min-w-0 truncate`).
 * Everything else is content that cannot shrink: a 16px chevron, a 13px icon,
 * a badge, five 7px cells, and count text. Two defects let the squeeze land on
 * those instead:
 *
 *  - `basename` split on `/` only, so a Windows `C:\…` or UNC `\\srv\…` path
 *    named the row with the WHOLE path and every row was wider than the card.
 *  - the count wrapper (`min-w-[8ch]`, no `shrink-0`) and the metadata rail
 *    (`min-w-0`) were both allowed to shrink below content they cannot shrink,
 *    so on an over-wide row the cells overflowed right and the count — with
 *    `justify-end` — overflowed left, and the two drew on top of each other.
 *
 * happy-dom lays nothing out, so the layout half of this file asserts the flex
 * contract that produces (or prevents) the overlap; the real-Chromium
 * measurement of the same rows lives in
 * `scripts/capture-diff-card-count-overlap.mjs`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, cleanup, within, fireEvent } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({ names: [] as string[] }))

/* Pierre paints the open header into a shadow root behind a lazy chunk that
 * never resolves under vitest; the mock records the `name` the row hands it,
 * which is the string Pierre titles its header with. */
vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: ({ oldFile }: { oldFile: { name: string } }) => {
    hoisted.names.push(oldFile.name)
    return <div data-testid="pierre-pair" />
  },
}))

import FileChangeChips from '../components/FileChangeChips'
import { __resetStagingForTests } from '../components/pierreStaging'

const change = (path: string, before: string, after: string) => ({ path, before, after })
const lines = (prefix: string, n: number) => Array.from({ length: n }, (_, i) => `${prefix}${i}`).join('\n')

const WIN = 'C:\\Users\\me\\.kiro\\crew\\workspace\\notes.md'
const UNC = '\\\\fileserver\\share\\team\\report.md'
/* On POSIX a backslash is an ordinary filename character; this row must keep
 * it rather than be split into a nonexistent nested path. */
const POSIX_BACKSLASH = '/tmp/odd\\name.txt'

/* A backslash inside a CSS attribute string is an escape, so a Windows path
 * must be escaped before it can select its own row. */
const css = (path: string) => path.replace(/\\/g, '\\\\')
const header = (c: HTMLElement, path: string) =>
  c.querySelector(`[data-testid="fcc-header-${css(path)}"]`) as HTMLElement
const filenameOf = (c: HTMLElement, path: string) =>
  header(c, path).querySelector('[data-fcc-filename]') as HTMLElement

beforeEach(() => {
  hoisted.names.length = 0
  localStorage.clear()
  __resetStagingForTests()
  cleanup()
})

describe('row name for a Windows path', () => {
  it('names a drive-letter row by its basename and keeps the full path on the tooltip', () => {
    const { container } = render(<FileChangeChips fileChanges={[change(WIN, 'a', 'b')]} />)
    expect(filenameOf(container, WIN)).toHaveTextContent(/^notes\.md$/)
    expect(filenameOf(container, WIN)).toHaveAttribute('title', WIN)
    // The tooltip row and the control labels keep the whole path: the basename
    // is a display choice, not a loss of information.
    expect(container.querySelector(`[data-testid="fcc-row-${css(WIN)}"]`)).toHaveAttribute('title', WIN)
    expect(container.querySelector(`[data-testid="fcc-toggle-${css(WIN)}"]`))
      .toHaveAttribute('aria-label', `Show or hide the diff for ${WIN}`)
  })

  it('names a UNC row by its basename', () => {
    const { container } = render(<FileChangeChips fileChanges={[change(UNC, 'a', 'b')]} />)
    expect(filenameOf(container, UNC)).toHaveTextContent(/^report\.md$/)
    expect(filenameOf(container, UNC)).toHaveAttribute('title', UNC)
  })

  it('leaves a POSIX name that contains a backslash whole', () => {
    const { container } = render(<FileChangeChips fileChanges={[change(POSIX_BACKSLASH, 'a', 'b')]} />)
    expect(filenameOf(container, POSIX_BACKSLASH)).toHaveTextContent(/^odd\\name\.txt$/)
  })

  it('hands the Open control the full path, so the file still opens', () => {
    const onFileOpen = vi.fn()
    const { container } = render(<FileChangeChips fileChanges={[change(WIN, 'a', 'b')]} onFileOpen={onFileOpen} />)
    const open = within(header(container, WIN)).getByLabelText(`Open ${WIN} in side panel`)
    expect(open).toHaveTextContent(/^notes\.md$/)
    fireEvent.click(open)
    expect(onFileOpen).toHaveBeenCalledWith(WIN)
  })

  it('uses the basename on every row surface: truncated row, minimal pills, and the name Pierre titles its header with', () => {
    const truncated = { ...change(WIN, 'same', 'same'), truncated: true, snapshot_limit_chars: 200_000 }
    const demoted = { ...change(UNC, '', ''), truncated: true, content_omitted: true, turn_budget_chars: 400_000 }

    const card = render(<FileChangeChips fileChanges={[truncated]} />)
    expect(card.container.querySelector('[data-fcc-filename]')).toHaveTextContent(/^notes\.md$/)
    card.unmount()

    const pills = render(<FileChangeChips fileChanges={[change(WIN, 'a', 'a\nb'), demoted]} style="minimal" />)
    // Demoted pill: its face is the filename.
    expect(pills.getByRole('button', { name: UNC }).querySelector('[data-fcc-pill-filename]')).toHaveTextContent(/^report\.md$/)
    // Stats pill beside a demoted one names its file too, and its hover label
    // is the same basename.
    const stats = pills.getByRole('button', { name: WIN })
    expect(stats.querySelector('[data-fcc-pill-filename]')).toHaveTextContent(/^notes\.md$/)
    expect(stats.parentElement?.firstElementChild).toHaveTextContent(/^notes\.md$/)
    pills.unmount()

    const open = render(<FileChangeChips fileChanges={[change(WIN, 'a', 'b')]} />)
    fireEvent.click(open.container.querySelector(`[data-testid="fcc-toggle-${css(WIN)}"]`)!)
    expect(hoisted.names).toEqual(['notes.md'])
  })
})

describe('collapsed row flex contract (count never under the diffstat cells)', () => {
  /* 965 removed / 1032 added: the count the issue was reported with, wider
   * than the 8ch column the count wrapper reserves. */
  const wide = change('/docs/release-notes.md', lines('r', 965), lines('a', 1032))

  it('reserves the count its full width: the wrapper is shrink-0, so a wide count widens the column instead of sliding under the cells', () => {
    const { container } = render(<FileChangeChips fileChanges={[wide]} />)
    const count = header(container, wide.path).querySelector('[data-fcc-count]') as HTMLElement
    expect(count).toHaveTextContent('-965+1032')
    expect(count).toHaveClass('shrink-0')
    // The 8ch floor stays: a short count still right-aligns in a column of the
    // same width as the row above it.
    expect(count).toHaveClass('min-w-[8ch]')
  })

  it('never lets the metadata rail shrink below its cells, which cannot shrink', () => {
    const { container } = render(<FileChangeChips fileChanges={[wide]} />)
    const rail = container.querySelector('[data-testid="fcc-metadata"]') as HTMLElement
    expect(rail).toHaveClass('shrink-0')
    // `min-w-0` was the override that let the rail shrink below its content.
    expect(rail).not.toHaveClass('min-w-0')
    // A reserved basis would re-create the 320px filename crush that #8316's
    // review removed: the rail is exactly as wide as what it holds.
    expect(rail.style.flexBasis).toBe('')
    expect(rail.className).not.toMatch(/\b(basis-|w-\[)/)
  })

  it('leaves the filename as the ONLY flex item in the header that may shrink', () => {
    const { container } = render(<FileChangeChips fileChanges={[wide]} artifactPaths={new Set([wide.path])} />)
    const items = Array.from(header(container, wide.path).children) as HTMLElement[]
    expect(items.length).toBeGreaterThanOrEqual(5)
    const shrinkable = items.filter(el => !el.classList.contains('shrink-0'))
    expect(shrinkable).toHaveLength(1)
    expect(shrinkable[0]).toHaveAttribute('data-fcc-filename')
    expect(shrinkable[0]).toHaveClass('min-w-0', 'truncate')
  })
})
