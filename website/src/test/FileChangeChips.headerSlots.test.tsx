/**
 * The expanded card hands Pierre three header slots — a chevron prefix, an
 * Open-file suffix next to the filename, and a metadata cell carrying the
 * artifact badge plus the diffstat bar — and delegates header clicks back to
 * itself, routing them by `composedPath()`.
 *
 * None of that is reachable through the real component: Pierre paints the
 * header into a shadow root behind a lazy chunk that never resolves under
 * vitest. So this mocks `PierreFilePair` with a header shaped like the one
 * Pierre emits (a `[data-diffs-header]` band holding a `[data-title]` filename
 * node, the slots, and a body below it) and drives real clicks through it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, cleanup, within } from '@testing-library/react'

const hoisted = vi.hoisted(() => ({ options: [] as { collapsed: boolean; diffStyle?: string }[] }))

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: ({ oldFile, options, renderHeaderPrefix, renderHeaderFilenameSuffix, renderHeaderMetadata }: {
    oldFile: { name: string }
    options: { collapsed: boolean; diffStyle?: string }
    renderHeaderPrefix?: () => React.ReactNode
    renderHeaderFilenameSuffix?: () => React.ReactNode
    renderHeaderMetadata?: () => React.ReactNode
  }) => {
    hoisted.options.push(options)
    return (
      <div data-testid="pierre-pair">
        <div data-diffs-header="">
          {renderHeaderPrefix?.()}
          <span data-title="">{oldFile.name}</span>
          {renderHeaderFilenameSuffix?.()}
          {renderHeaderMetadata?.()}
        </div>
        <pre data-code="">body</pre>
      </div>
    )
  },
}))

import FileChangeChips from '../components/FileChangeChips'
import { __resetStagingForTests } from '../components/pierreStaging'

const change = (path: string, before: string, after: string) => ({ path, before, after })
const latest = () => hoisted.options[hoisted.options.length - 1]
const pierreHeader = (c: HTMLElement) => c.querySelector('[data-diffs-header]') as HTMLElement
const collapsedHeader = (c: HTMLElement) => c.querySelector('[data-testid^="fcc-header-"]') as HTMLElement
const toggle = (c: HTMLElement, path = '/a.ts') =>
  fireEvent.click(c.querySelector(`[data-testid="fcc-toggle-${path}"]`)!)
const cells = (c: HTMLElement, cls: string) => c.querySelectorAll(`.${cls}`).length

beforeEach(() => {
  hoisted.options.length = 0
  // The card's split/unified layout persists app-wide (`mc-diff-split`);
  // start each test from the unseeded default.
  localStorage.clear()
  // Row mounting is staged through module-level queue state, so a test that
  // spends the eager budget would leave the next one rendering placeholders
  // instead of Pierre — these tests are about the header Pierre paints.
  __resetStagingForTests()
  cleanup()
})

describe('expanded row header slots', () => {
  it('shows the basename without mounting Pierre and keeps the full path on the row tooltip', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/deep/nested/index.ts', 'a', 'b')]} />,
    )
    expect(collapsedHeader(container).textContent).toContain('index.ts')
    expect(container.querySelector('[data-testid="pierre-pair"]')).toBeNull()
    expect(container.querySelector('[data-testid="fcc-row-/deep/nested/index.ts"]')?.getAttribute('title'))
      .toBe('/deep/nested/index.ts')
  })

  it('offers a keyboard-reachable Open control naming the file, only when a handler exists', () => {
    const onFileOpen = vi.fn()
    const { container, rerender } = render(
      <FileChangeChips fileChanges={[change('/src/a.ts', 'a', 'b')]} onFileOpen={onFileOpen} />,
    )
    const open = within(collapsedHeader(container)).getByLabelText('Open /src/a.ts in side panel')
    expect(open).toHaveAttribute('title', '/src/a.ts')
    fireEvent.click(open)
    expect(onFileOpen).toHaveBeenCalledWith('/src/a.ts')

    // Without a handler the affordance is absent rather than inert.
    rerender(<FileChangeChips fileChanges={[change('/src/a.ts', 'a', 'b')]} />)
    expect(within(collapsedHeader(container)).queryByLabelText(/in side panel/)).toBeNull()
  })

  it('badges only the rows the session tracks as artifacts', () => {
    const { container } = render(
      <FileChangeChips
        fileChanges={[change('/notes.md', 'a', 'b'), change('/src/a.ts', 'a', 'b')]}
        artifactPaths={new Set(['/notes.md'])}
      />,
    )
    const badgesIn = (path: string) => within(
      container.querySelector(`[data-testid="fcc-row-${path}"]`) as HTMLElement,
    ).queryAllByText('Artifact')
    expect(badgesIn('/notes.md')).toHaveLength(1)
    expect(badgesIn('/notes.md')[0]).toHaveAttribute(
      'title',
      'This document is tracked as a session artifact, not a source-file change',
    )
    expect(badgesIn('/src/a.ts')).toHaveLength(0)
    const secondaryIn = (path: string) => container
      .querySelector(`[data-testid="fcc-row-${path}"] [data-fcc-secondary-metadata]`)
    expect(secondaryIn('/notes.md')).toHaveClass('max-[420px]:hidden')
    expect(secondaryIn('/src/a.ts')).not.toHaveClass('max-[420px]:hidden')
  })

  it('keeps the lightweight metadata rail fluid for narrow rows', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/src/long-component-name.ts', 'a', 'b')]} />,
    )
    const metadata = container.querySelector<HTMLElement>('[data-testid="fcc-metadata"]')!
    expect(metadata.style.flexBasis).toBe('')
    expect(metadata.className).not.toContain('shrink-0')
  })
})

describe('card split/unified toggle', () => {
  it('mounts no Pierre rows until each file is opened', () => {
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'b'), change('/b.ts', 'a', 'b')]} />,
    )
    expect(hoisted.options).toHaveLength(0)
    toggle(container, '/a.ts')
    expect(hoisted.options).toHaveLength(1)
    toggle(container, '/b.ts')
    expect(hoisted.options).toHaveLength(2)
  })

  it('reveals more closed rows without mounting Pierre', () => {
    const files = Array.from({ length: 12 }, (_, i) => change(`/f${i}.ts`, 'a', 'b'))
    const { getByText } = render(<FileChangeChips fileChanges={files} />)
    expect(hoisted.options).toHaveLength(0)
    fireEvent.click(getByText('Show 4 more'))
    expect(hoisted.options).toHaveLength(0)
  })

  it('flips every mounted row between split and unified and persists the choice', () => {
    const { container, getByLabelText } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'b'), change('/b.ts', 'a', 'b')]} />,
    )
    toggle(container, '/a.ts')
    toggle(container, '/b.ts')
    // Unseeded default is split — the shared `mc-diff-split` preference's
    // default — and every opened row receives it.
    const styles = () => hoisted.options.map(o => o.diffStyle)
    expect(styles()).toEqual(['split', 'split'])

    hoisted.options.length = 0
    fireEvent.click(getByLabelText('Switch to unified view'))
    expect(styles()).toEqual(['unified', 'unified'])
    // The choice lands in the shared preference (#6024), not card-local state.
    expect(localStorage.getItem('mc-diff-split')).toBe('0')
    expect(within(container as HTMLElement).getByLabelText('Switch to split view')).toBeInTheDocument()
  })

  it('seeds opened rows from the shared mc-diff-split preference', () => {
    localStorage.setItem('mc-diff-split', '0')
    const { container, getByLabelText } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} />,
    )
    expect(hoisted.options).toHaveLength(0)
    toggle(container)
    expect(hoisted.options[0].diffStyle).toBe('unified')
    expect(getByLabelText('Switch to split view')).toBeInTheDocument()
  })
})

describe('diffstat bar', () => {
  const bar = (before: string, after: string) => {
    const { container } = render(<FileChangeChips fileChanges={[change('/a.ts', before, after)]} />)
    return { green: cells(container, 'bg-ok'), red: cells(container, 'bg-danger'), container }
  }
  const dup = (line: string, n: number) => Array.from({ length: n }, () => line).join('\n')

  it('fills the bar with additions when nothing was removed', () => {
    expect(bar('', dup('a', 4))).toMatchObject({ green: 5, red: 0 })
  })

  it('fills the bar with removals when nothing was added', () => {
    expect(bar(dup('a', 4), '')).toMatchObject({ green: 0, red: 5 })
  })

  it('splits the five cells by proportion, favouring the larger side', () => {
    // 3 added / 7 removed rounds to 2 + 4 cells, one over budget; the smaller
    // side gives the cell back, so the majority stays visually dominant.
    const before = Array.from({ length: 7 }, (_, i) => `r${i}`).join('\n')
    const { green, red } = bar(before, 'n0\nn1\nn2')
    expect({ green, red }).toEqual({ green: 2, red: 3 })
  })

  it('never overflows five cells on an even split', () => {
    const { green, red } = bar('a\nb', 'c\nd')
    expect(green + red).toBe(5)
    expect(green).toBeGreaterThan(0)
    expect(red).toBeGreaterThan(0)
  })

  it('hides the bar entirely when nothing changed, rather than showing a dead row', () => {
    const { green, red, container } = bar('same', 'same')
    expect({ green, red }).toEqual({ green: 0, red: 0 })
    expect(cells(container, 'bg-border')).toBe(0)
  })
})

describe('header click routing', () => {
  const rowIsOpen = (c: HTMLElement) =>
    c.querySelector('[data-testid^="fcc-toggle-"]')!.getAttribute('aria-expanded') === 'true'

  it('opens the file from the collapsed filename without mounting Pierre', () => {
    const onFileOpen = vi.fn()
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} onFileOpen={onFileOpen} />,
    )
    fireEvent.click(within(collapsedHeader(container)).getByLabelText('Open /a.ts in side panel'))
    expect(onFileOpen).toHaveBeenCalledWith('/a.ts')
    expect(rowIsOpen(container)).toBe(false)
    expect(container.querySelector('[data-testid="pierre-pair"]')).toBeNull()
  })

  it('opens from collapsed whitespace and closes from Pierre header whitespace', () => {
    const onFileOpen = vi.fn()
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} onFileOpen={onFileOpen} />,
    )
    fireEvent.click(collapsedHeader(container))
    expect(rowIsOpen(container)).toBe(true)
    expect(latest().collapsed).toBe(false)
    expect(onFileOpen).not.toHaveBeenCalled()

    fireEvent.click(pierreHeader(container))
    expect(rowIsOpen(container)).toBe(false)
  })

  it('leaves the row alone when the filename is clicked with no open handler', () => {
    const { container } = render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} />)
    const filename = collapsedHeader(container).querySelector('[title="/a.ts"]')!
    expect(() => fireEvent.click(filename)).not.toThrow()
    expect(rowIsOpen(container)).toBe(false)
  })

  it('ignores clicks on the diff body, so selecting code never collapses it', () => {
    const { container } = render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} />)
    toggle(container)
    expect(rowIsOpen(container)).toBe(true)
    fireEvent.click(container.querySelector('[data-code]')!)
    expect(rowIsOpen(container)).toBe(true)
  })

  it('handles a chevron click once, not twice through the header delegate', () => {
    const { container } = render(<FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} />)
    toggle(container)
    expect(rowIsOpen(container)).toBe(true)
    expect(latest().collapsed).toBe(false)
  })

  it('does not route Pierre’s Open button through the header delegate', () => {
    const onFileOpen = vi.fn()
    const { container } = render(
      <FileChangeChips fileChanges={[change('/a.ts', 'a', 'b')]} onFileOpen={onFileOpen} />,
    )
    toggle(container)
    fireEvent.click(within(pierreHeader(container)).getByLabelText('Open /a.ts in side panel'))
    expect(onFileOpen).toHaveBeenCalledTimes(1)
    expect(rowIsOpen(container)).toBe(true)
  })
})
