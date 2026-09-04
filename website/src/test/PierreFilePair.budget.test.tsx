import { beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import type { FileContents } from '@pierre/diffs'
import { PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE } from '../pierre/config'

const impl = vi.hoisted(() => ({ calls: 0 }))

vi.mock('../pierre/PierreImpl', () => ({
  PierreFilePairImpl: () => {
    impl.calls++
    return <div data-testid="pierre-file-pair" />
  },
}))

import { PierreFilePair } from '../pierre'

const file = (contents: string): FileContents => ({ name: 'generated.ts', contents })
const lines = (count: number) => Array.from({ length: count }, (_, i) => `const v${i} = ${i}`).join('\n')

beforeEach(() => {
  cleanup()
  impl.calls = 0
})

describe('PierreFilePair render budget', () => {
  it('keeps bounded pairs on the lazy Pierre implementation', async () => {
    render(<PierreFilePair oldFile={file('before')} newFile={file('after')} />)

    expect(await screen.findByTestId('pierre-file-pair')).toBeInTheDocument()
    expect(impl.calls).toBe(1)
  })

  it('keeps both oversized sides byte-for-byte without mounting Pierre', () => {
    const before = 'before sentinel\nunchanged'
    const after = `${lines(PIERRE_FILE_PAIR_MAX_LINES_PER_SIDE + 1)}
after sentinel`
    const { container } = render(
      <PierreFilePair oldFile={file(before)} newFile={file(after)} options={{ diffStyle: 'split' }} />,
    )

    expect(impl.calls).toBe(0)
    expect(container.querySelector('[data-pierre-plain-file-pair]')).toBeInTheDocument()
    expect(container.querySelector('[data-diffs-header]')).not.toBeInTheDocument()
    expect(screen.getByText('Large file — simplified view')).toBeInTheDocument()
    expect(container.querySelector('[data-pierre-plain-side="old"]')).toHaveAttribute('tabindex', '0')
    expect(container.querySelector('[data-pierre-plain-side="old"]')).toHaveAttribute('role', 'region')
    expect(container.querySelector('[data-pierre-plain-side="old"]')?.textContent).toBe(before)
    expect(container.querySelector('[data-pierre-plain-side="new"]')?.textContent).toBe(after)
    expect(screen.queryByTestId('pierre-file-pair')).not.toBeInTheDocument()
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
