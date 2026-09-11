import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { FileContents } from '@pierre/diffs'
import { PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE } from '../pierre/config'

const impl = vi.hoisted(() => ({ calls: 0, patchCalls: 0 }))

vi.mock('../pierre/PierreImpl', () => ({
  PierreFilePairImpl: ({ oldFile, newFile }: { oldFile: FileContents | null; newFile: FileContents | null }) => {
    impl.calls++
    return (
      <div data-testid="pierre-file-pair">
        {oldFile && <div data-line-type="deletion">{oldFile.contents}</div>}
        {newFile && <div data-line-type="addition">{newFile.contents}</div>}
      </div>
    )
  },
  PierrePatchImpl: ({ patch }: { patch: string }) => {
    impl.patchCalls++
    const lines = patch.split('\n')
    return (
      <div data-testid="pierre-pair-patch">
        {lines.filter(l => l.startsWith('-') && !l.startsWith('---')).map((l, i) => (
          <div key={`d${i}`} data-line-type="deletion">{l.slice(1)}</div>
        ))}
        {lines.filter(l => l.startsWith('+') && !l.startsWith('+++')).map((l, i) => (
          <div key={`a${i}`} data-line-type="addition">{l.slice(1)}</div>
        ))}
      </div>
    )
  },
}))

// jsdom has no `Worker`; the client wrapper is replaced with a synchronous
// jsdiff-shaped stub. The REAL worker path is covered by the manual-verification
// steps in the PR — this mock keeps the state machine + patch-path rendering
// under test.
vi.mock('../pierre/diffOffThread', () => ({
  computePairPatch: (oldFile: FileContents | null, newFile: FileContents | null) =>
    Promise.resolve(
      [
        `--- ${oldFile?.name ?? 'file'}`,
        `+++ ${newFile?.name ?? 'file'}`,
        '@@ -1 +1 @@',
        ...(oldFile ? oldFile.contents.split('\n').slice(0, 1).map(l => `-${l}`) : []),
        ...(newFile ? newFile.contents.split('\n').slice(0, 1).map(l => `+${l}`) : []),
      ].join('\n'),
    ),
}))

import { PierreFilePair } from '../pierre'

const file = (contents: string): FileContents => ({ name: 'generated.ts', contents })
const lines = (count: number) => Array.from({ length: count }, (_, i) => `const v${i} = ${i}`).join('\n')

beforeEach(() => {
  cleanup()
  impl.calls = 0
  impl.patchCalls = 0
})

describe('PierreFilePair render budget', () => {
  it('keeps bounded pairs, including exactly 400 lines, on the lazy Pierre implementation', async () => {
    render(<PierreFilePair oldFile={file(lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE))} newFile={file('after')} />)

    expect(await screen.findByTestId('pierre-file-pair')).toBeInTheDocument()
    expect(impl.calls).toBe(1)
  })

  it('keeps every oversized line without mounting Pierre, and marks the change region', () => {
    const before = 'before sentinel\nunchanged'
    const after = `${lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1)}
after sentinel`
    const { container } = render(
      <PierreFilePair oldFile={file(before)} newFile={file(after)} options={{ diffStyle: 'split' }} />,
    )

    expect(impl.calls).toBe(0)
    expect(container.querySelector('[data-pierre-plain-file-pair]')).toBeInTheDocument()
    expect(container.querySelector('[data-diffs-header]')).not.toBeInTheDocument()
    expect(screen.getByText('Large file — shaded band marks region, not exact lines')).toBeInTheDocument()
    const optInButton = screen.getByRole('button', { name: 'Show line-by-line diff' })
    expect(optInButton).toBeInTheDocument()
    // The opt-in strip must live OUTSIDE the bounded scroller: inside it, the
    // control scrolls out of view (and starts hidden once the fallback opens
    // scrolled to the first change).
    expect(optInButton.closest('[data-pierre-plain-content]')).toBeNull()
    expect(container.querySelector('[data-pierre-plain-side="old"]')).toHaveAttribute('tabindex', '0')
    expect(container.querySelector('[data-pierre-plain-side="old"]')).toHaveAttribute('role', 'region')
    // Every source line is still present on each side; remove only the
    // non-colour markers prefixed to rows the scan identifies as changed.
    const sourceText = (pre: Element | null, marker: string) =>
      (pre?.textContent ?? '').replaceAll(`${marker} `, '')
    expect(sourceText(container.querySelector('[data-pierre-plain-side="old"]'), '−')).toBe(before)
    expect(sourceText(container.querySelector('[data-pierre-plain-side="new"]'), '+')).toBe(after)
    // The two files share no lines, so one constant-size region span preserves
    // the whole range instead of building one DOM node per line.
    const regions = container.querySelectorAll('[data-pierre-plain-side="new"] [data-change-region]')
    expect(regions).toHaveLength(1)
    expect(regions[0].textContent).toContain('after sentinel')
    expect(screen.queryByTestId('pierre-file-pair')).not.toBeInTheDocument()
  })

  it('anchors at the identified changed line without marking line 1', () => {
    // A small edit deep in a large file (issue #9926): line ~870 of ~1000.
    const src = Array.from({ length: PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 600 }, (_, i) => `const v${i} = ${i}`)
    const before = src.join('\n')
    const editAt = 869
    const afterArr = [...src]
    afterArr[editAt] = 'const v869 = 869 /* patched */'
    const { container } = render(
      <PierreFilePair oldFile={file(before)} newFile={file(afterArr.join('\n'))} options={{ diffStyle: 'split' }} />,
    )

    expect(impl.calls).toBe(0)
    const newSide = container.querySelector('[data-pierre-plain-side="new"]')
    const content = container.querySelector('[data-pierre-plain-content]')
    expect(content).toHaveClass('relative')
    const changed = newSide?.querySelector('[data-changed-line]')
    // The identified row is the scroll anchor. Line 1 remains plain text
    // outside the region band.
    expect(newSide?.querySelectorAll('[data-change-region]')).toHaveLength(1)
    expect(newSide?.querySelectorAll('[data-changed-line]')).toHaveLength(1)
    expect(changed?.textContent).toContain('/* patched */')
    expect(changed?.textContent).not.toContain('const v0 = 0')
    expect(newSide?.textContent?.startsWith('const v0 = 0')).toBe(true)
  })

  it('keeps an empty changed boundary row byte-for-byte', () => {
    const beforeLines = Array.from(
      { length: PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 600 },
      (_, i) => `const v${i} = ${i}`,
    )
    const afterLines = [...beforeLines]
    afterLines[869] = ''
    const before = beforeLines.join('\n')
    const after = afterLines.join('\n')
    const { container } = render(
      <PierreFilePair oldFile={file(before)} newFile={file(after)} options={{ diffStyle: 'split' }} />,
    )

    const newSide = container.querySelector('[data-pierre-plain-side="new"]')
    expect((newSide?.textContent ?? '').replaceAll('+ ', '')).toBe(after)
    expect(newSide?.textContent).not.toContain('\u200b')
  })

  it('uses a neutral region band and diff colors only on boundary rows', () => {
    const src = Array.from({ length: PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 600 }, (_, i) => `const v${i} = ${i}`)
    const top = 4
    const bottom = src.length - 5
    const after = [...src]
    after[top] = `${src[top]} /* patched top */`
    after[bottom] = `${src[bottom]} /* patched bottom */`
    const { container } = render(
      <PierreFilePair oldFile={file(src.join('\n'))} newFile={file(after.join('\n'))} options={{ diffStyle: 'split' }} />,
    )

    for (const [side, diffClass, textClass] of [
      ['old', 'bg-[var(--diff-del)]', 'text-[var(--diff-del-text)]'],
      ['new', 'bg-[var(--diff-add)]', 'text-[var(--diff-add-text)]'],
    ] as const) {
      const region = container.querySelector(`[data-pierre-plain-side="${side}"] [data-change-region]`)
      expect(region).toHaveClass('bg-bg-hover')
      expect(region).not.toHaveClass('bg-[var(--diff-add)]')
      expect(region).not.toHaveClass('bg-[var(--diff-del)]')
      expect(region).not.toHaveClass('text-[var(--diff-add-text)]')
      expect(region).not.toHaveClass('text-[var(--diff-del-text)]')
      const changedRows = region?.querySelectorAll('[data-changed-line]') ?? []
      expect(changedRows).toHaveLength(2)
      for (const row of changedRows) {
        expect(row).toHaveClass(diffClass)
        expect(row).toHaveClass(textClass)
      }
    }
  })

  it('renders every line plain when the two sides are identical', () => {
    const same = lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1)
    const { container } = render(
      <PierreFilePair oldFile={file(same)} newFile={file(same)} options={{ diffStyle: 'split' }} />,
    )
    expect(impl.calls).toBe(0)
    // Identical content produces no region, so nothing is marked and the
    // band-specific copy is absent.
    expect(container.querySelectorAll('[data-change-region]')).toHaveLength(0)
    expect(container.querySelectorAll('[data-changed-line]')).toHaveLength(0)
    expect(screen.getByText('Large file — simplified view')).toBeInTheDocument()
    expect(screen.queryByText('Large file — shaded band marks region, not exact lines')).not.toBeInTheDocument()
  })

  it('renders changed-line classifications for an oversized pair only after explicit opt-in', async () => {
    const user = userEvent.setup()
    const { container } = render(
      <PierreFilePair oldFile={file('before')} newFile={file(lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1))} />,
    )

    expect(impl.calls).toBe(0)
    expect(impl.patchCalls).toBe(0)
    expect(container.querySelector('[data-line-type]')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Show line-by-line diff' }))

    expect(await screen.findByTestId('pierre-pair-patch')).toBeInTheDocument()
    // The synchronous whole-file diff never mounts for the oversized path —
    // the click computes a patch off-thread and renders it hunk-based.
    expect(impl.calls).toBe(0)
    expect(impl.patchCalls).toBe(1)
    expect(container.querySelector('[data-line-type="deletion"]')).toHaveTextContent('before')
    expect(container.querySelector('[data-line-type="addition"]')).toHaveTextContent('const v0 = 0')
  })

  it('opting one oversized pair in does not diff its siblings', async () => {
    const user = userEvent.setup()
    const bigA = file(lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1))
    const bigB: FileContents = { name: 'other.ts', contents: lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 2) }
    render(
      <>
        <PierreFilePair oldFile={file('a-before')} newFile={bigA} />
        <PierreFilePair oldFile={{ name: 'other.ts', contents: 'b-before' }} newFile={bigB} />
      </>,
    )

    const buttons = screen.getAllByRole('button', { name: 'Show line-by-line diff' })
    expect(buttons).toHaveLength(2)

    await user.click(buttons[0])

    expect(await screen.findByTestId('pierre-pair-patch')).toBeInTheDocument()
    // Exactly one pair rendered its diff; the sibling still shows its plain
    // fallback with the button.
    expect(impl.patchCalls).toBe(1)
    expect(screen.getAllByTestId('pierre-pair-patch')).toHaveLength(1)
    expect(screen.getAllByRole('button', { name: 'Show line-by-line diff' })).toHaveLength(1)
  })

  it('preserves a collapsed row header and every injected control', () => {
    const after = lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1)
    const { container } = render(
      <PierreFilePair
        oldFile={file('before')}
        newFile={file(after)}
        options={{ collapsed: true, disableFileHeader: false }}
        fallbackContentStyle={{ maxHeight: 376, overflowY: 'auto' }}
        renderHeaderPrefix={() => <span data-testid="prefix" />}
        renderHeaderFilenameSuffix={() => <span data-testid="suffix" />}
        renderHeaderMetadata={() => <span data-testid="metadata" />}
      />,
    )

    expect(impl.calls).toBe(0)
    expect(container.querySelector('[data-diffs-header]')).toBeInTheDocument()
    expect(container.querySelector('[data-title]')).toHaveTextContent('generated.ts')
    expect(screen.getByTestId('prefix')).toBeInTheDocument()
    expect(screen.getByTestId('suffix')).toBeInTheDocument()
    expect(screen.getByTestId('metadata')).toBeInTheDocument()
    expect(screen.getByText('Large file — simplified view')).toBeInTheDocument()
    expect(screen.queryByText('Large file — shaded band marks region, not exact lines')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Show line-by-line diff' })).not.toBeInTheDocument()
    expect(container.querySelector('[data-pierre-plain-side]')).not.toBeInTheDocument()
  })

  it('keeps unified sides separated horizontally and applies caller-owned sizing', () => {
    const after = lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1)
    const { container } = render(
      <PierreFilePair
        oldFile={file('before')}
        newFile={file(after)}
        options={{ diffStyle: 'unified' }}
        fallbackContentStyle={{ maxHeight: 376, overflowY: 'auto' }}
      />,
    )

    const content = container.querySelector('[data-pierre-plain-content]') as HTMLElement
    const sections = container.querySelectorAll('section')
    expect(content.style.maxHeight).toBe('376px')
    expect(content.style.overflowY).toBe('auto')
    expect(sections[1].className).toContain('border-t')
    expect(sections[1].className).not.toContain('md:border-l')
  })
})
