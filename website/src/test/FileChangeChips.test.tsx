import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import FileChangeChips, { countLines, headerClickAction, type FileChangeEntry } from '../components/FileChangeChips'

const change = (path: string, before: string, after: string) => ({ path, before, after })
const rows = (c: HTMLElement) => c.querySelectorAll('[data-testid^="fcc-row-"]')

/* The expanded style renders a lightweight header per closed file and mounts
 * Pierre only after disclosure. Two consequences for this suite:
 *
 *  - Closed filenames, counts, diffstat cells and controls are ordinary DOM and
 *    covered by the focused header tests. Pierre's open header still lives in a
 *    shadow root behind a lazy chunk that never resolves in this suite.
 *  - The line counting used by both header forms is a pure exported function,
 *    so it is tested directly instead of through presentation details.
 *
 * Header parity and mount lifecycle are covered in the focused companion tests. */
describe('countLines', () => {
  it('counts pure additions', () => {
    expect(countLines('a', 'a\nb\nc')).toEqual({ added: 2, removed: 0 })
  })

  it('counts pure removals', () => {
    expect(countLines('a\nb\nc', 'a')).toEqual({ added: 0, removed: 2 })
  })

  it('reports a pure move as +N/-N, not 0/0', () => {
    // LCS attributes a moved line to both sides; a multiset count would call
    // this unchanged, which reads as "nothing happened" on a real reorder.
    expect(countLines('a\nb', 'b\na')).toEqual({ added: 1, removed: 1 })
  })

  it('reports nothing when the content is identical', () => {
    expect(countLines('same\ntext', 'same\ntext')).toEqual({ added: 0, removed: 0 })
  })

  it('counts a mixed edit', () => {
    expect(countLines('one\ntwo\nthree', 'one\ntwo-edited\nthree\nfour')).toEqual({ added: 2, removed: 1 })
  })

  it('treats empty content as zero lines, not one phantom line', () => {
    // ''.split('\n') is [''], which would mis-count a new file as +1/-1.
    expect(countLines('a\nb', '')).toEqual({ added: 0, removed: 2 })
    expect(countLines('', 'a')).toEqual({ added: 1, removed: 0 })
  })

  /* Past 1M LCS cells the counter drops to a multiset count to bound cost.
   * That fallback is cheaper and measurably weaker, so these pin what it
   * reports at that size — including the one case where it disagrees with
   * LCS — rather than restating the expectations above. */
  const numbered = (n: number) => Array.from({ length: n }, (_, i) => `line-${i}`)

  it('counts adds and removes past the LCS cell cap', () => {
    const before = numbered(1100)
    const after = [...before.slice(0, 1095), 'new-a', 'new-b', 'new-c']
    expect(before.length * after.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), after.join('\n'))).toEqual({ added: 3, removed: 5 })
  })

  it('under-reports a pure reorder past the cap, unlike the LCS path below it', () => {
    const before = numbered(1200)
    const reordered = [...before].reverse()
    expect(before.length * reordered.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), reordered.join('\n'))).toEqual({ added: 0, removed: 0 })

    // The same reorder under the cap runs through LCS, which does attribute it.
    const small = numbered(40)
    expect(countLines(small.join('\n'), [...small].reverse().join('\n')).added).toBeGreaterThan(0)
  })

  it('nets out duplicate lines past the cap instead of counting every occurrence', () => {
    const before = [...numbered(1100), 'dup', 'dup', 'dup']
    const after = [...numbered(1100), 'dup']
    expect(before.length * after.length).toBeGreaterThan(1_000_000)
    expect(countLines(before.join('\n'), after.join('\n'))).toEqual({ added: 0, removed: 2 })
  })
})

describe('FileChangeChips', () => {
  it('renders nothing when fileChanges is empty', () => {
    const { container } = render(<FileChangeChips fileChanges={[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing when fileChanges is undefined', () => {
    // Component guards against undefined too — keeps consumers from needing
    // their own falsy guard.
    const { container } = render(<FileChangeChips fileChanges={undefined as unknown as FileChangeEntry[]} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders one row per file change', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), change('/b.py', 'x', 'y')]} />,
    )
    expect(rows(container)).toHaveLength(2)
    expect(container.querySelector('[data-testid="fcc-row-/a.ts"]')).toBeInTheDocument()
    expect(container.querySelector('[data-testid="fcc-row-/b.py"]')).toBeInTheDocument()
  })

  it('keys rows by path (no duplicate-key warnings)', () => {
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { container } = render(
      <FileChangeChips fileChanges={[change('/x.ts', 'a', 'b'), change('/y.ts', 'a', 'b')]} />,
    )
    expect(rows(container)).toHaveLength(2)
    expect(warn).not.toHaveBeenCalledWith(expect.stringContaining('same key'))
    warn.mockRestore()
  })

  it('spells out the file count and aggregate totals in the card header', () => {
    render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'a\nb'), change('/b.ts', 'a\nb', 'a')]} />)
    expect(screen.getByText('2 files changed')).toBeInTheDocument()
    // Singular forms, and no second ±pair on the right of the same row.
    expect(screen.getByText('1 addition')).toBeInTheDocument()
    expect(screen.getByText('1 removal')).toBeInTheDocument()
  })

  it('pluralises the header totals and omits a side that is zero', () => {
    render(<FileChangeChips fileChanges={[change('/new.ts', '', 'a\nb\nc')]} />)
    expect(screen.getByText('3 additions')).toBeInTheDocument()
    expect(screen.queryByText(/removal/)).not.toBeInTheDocument()
  })

  it('falls back to expanded for an unknown style value', () => {
    // Defensive default in the renderer map covers stale localStorage values
    // (e.g. legacy "tooltip"/"compact"/"full") until the migration runs.
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/legacy.ts', 'a', 'a\nb')]}
        // @ts-expect-error — intentional invalid style for the fallback path
        style="tooltip"
      />,
    )
    expect(container.querySelector('[data-testid="fcc-row-/legacy.ts"]')).toBeInTheDocument()
  })

  it('caps long lists at 8 rows behind a "Show N more" toggle', () => {
    const files = Array.from({ length: 11 }, (_, i) => change(`/f${i}.ts`, 'a', 'a\nb'))
    const { container } = render(<FileChangeChips fileChanges={files} />)
    expect(rows(container)).toHaveLength(8)
    // Header still reports the TRUE total.
    expect(screen.getByText('11 files changed')).toBeInTheDocument()
    // Expand reveals the remainder…
    fireEvent.click(screen.getByText('Show 3 more'))
    expect(rows(container)).toHaveLength(11)
    // …and collapses again.
    fireEvent.click(screen.getByText('Show less'))
    expect(rows(container)).toHaveLength(8)
  })

  it('does not cap lists at or below the threshold', () => {
    const files = Array.from({ length: 8 }, (_, i) => change(`/s${i}.ts`, 'a', 'b'))
    const { container } = render(<FileChangeChips fileChanges={files} />)
    expect(screen.queryByText(/Show \d+ more/)).not.toBeInTheDocument()
    expect(rows(container)).toHaveLength(8)
  })

  /* ── Header click routing. Pierre owns the header's inner nodes and its lazy
   *   chunk never resolves under vitest, so the real `[data-title]` cannot be
   *   clicked here — and a hand-stubbed `composedPath` does not survive React's
   *   dispatch, which resolves the target from that same path. The rule is
   *   therefore a pure function of the path and is tested as one; that the
   *   'toggle' verdict visibly collapses the row is Playwright's job. */
  const pathOf = (...sels: string[]) => sels.map(s => {
    const el = document.createElement('div')
    el.setAttribute(s, '')
    return el
  })

  it('routes a click on Pierre’s filename node to opening the file', () => {
    expect(headerClickAction(pathOf('data-title', 'data-diffs-header'))).toBe('open')
  })

  it('routes header whitespace to toggling, so the row has no dead zone', () => {
    expect(headerClickAction(pathOf('data-diffs-header'))).toBe('toggle')
  })

  it('routes lightweight header whitespace to toggling', () => {
    expect(headerClickAction(pathOf('data-fcc-header'))).toBe('toggle')
  })

  it('routes a lightweight filename to opening the file', () => {
    expect(headerClickAction(pathOf('data-fcc-filename', 'data-fcc-header'))).toBe('open')
  })

  it('ignores clicks below the header, so selecting code never collapses it', () => {
    expect(headerClickAction(pathOf('data-code'))).toBe('ignore')
    expect(headerClickAction([])).toBe('ignore')
  })

  it('ignores a filename node that is not inside a header', () => {
    // Order in the path is irrelevant, presence is what decides — but a title
    // without a header is not a header click at all.
    expect(headerClickAction(pathOf('data-title'))).toBe('ignore')
  })

  it('carries the full path as a tooltip so duplicate basenames stay distinguishable', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/src/a/index.ts', 'a', 'b'), change('/src/b/index.ts', 'a', 'b')]} />,
    )
    const titles = [...container.querySelectorAll('[data-testid^="fcc-row-"]')].map(r => r.getAttribute('title'))
    expect(titles).toEqual(['/src/a/index.ts', '/src/b/index.ts'])
  })

  /* ── Minimal style: hand-rolled pills, no Pierre, so still fully assertable
   *   here — and the one style that still routes clicks to `onOpenDiff`. */
  it('minimal style hides the filename in the pill but exposes a hover label', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/minimal.ts', 'a', 'a\nb')]} style="minimal" />,
    )
    const button = container.querySelector('button')
    expect(button?.textContent).not.toContain('minimal.ts')
    expect(container.textContent).toContain('minimal.ts')
    expect(button?.textContent).toContain('+1')
  })

  it('minimal style shows a no-changes caption when before === after', () => {
    render(<FileChangeChips fileChanges={[change('/same.ts', 'x', 'x')]} style="minimal" />)
    expect(screen.getByText('no changes')).toBeInTheDocument()
  })

  it('minimal style click triggers onOpenDiff with (path, after, before)', () => {
    const onOpenDiff = vi.fn()
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/min-click.ts', 'a', 'b')]}
        style="minimal"
        onOpenDiff={onOpenDiff}
      />,
    )
    fireEvent.click(container.querySelector('button')!)
    expect(onOpenDiff).toHaveBeenCalledWith('/min-click.ts', 'b', 'a')
  })

  it('does not throw on a minimal click when onOpenDiff is missing', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/no-handler.ts', 'a', 'b')]} style="minimal" />,
    )
    expect(() => fireEvent.click(container.querySelector('button')!)).not.toThrow()
  })
})
