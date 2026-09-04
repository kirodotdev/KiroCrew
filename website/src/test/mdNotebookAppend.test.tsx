/**
 * Regression tests for the Notes app's trailing click-to-append region (#3741).
 *
 * `Preview` derives its block indices from `body.split('\n')`, and that array
 * carries a trailing EMPTY segment whenever the body ends with a newline — which
 * is the normal shape of any file that came out of a git-backed vault. The
 * segment is not a line of content, so the append region has to insert AT its
 * slot; pointing one past it appended a blank line the user never typed.
 *
 * The indices asserted here are the contract `commitBlockEdit` /`splitBlockEdit`
 * in `MdNotebookPage.tsx` splice against, so `commitAppend` below mirrors that
 * splice to show the resulting text rather than only the index.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { Preview } from '../apps/md-notebook/Preview'
import type { EditRange } from '../apps/md-notebook/types'

function renderPreview(content: string, editRange: EditRange | null = null) {
  const onStartEdit = vi.fn()
  render(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={editRange}
      onStartEdit={onStartEdit}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
  return onStartEdit
}

/**
 * Click the trailing append region. Every block is a `Clickable` (role=button)
 * and the append region is pushed last, so it is the final one.
 */
async function clickAppend() {
  const blocks = screen.getAllByRole('button')
  await userEvent.click(blocks[blocks.length - 1])
}

/**
 * Mirrors the insertion path of `commitBlockEdit` in `MdNotebookPage.tsx`: it
 * splices against the WHOLE file split on newlines, with `count === 0` for an
 * insertion (`end < start`). Frontmatter is absent here, so `fmOffset()` is 0.
 */
function commitAppend(content: string, start: number, end: number, text: string): string {
  const lines = content.split('\n')
  const count = Math.max(0, end - start + 1)
  lines.splice(start, count, ...text.split('\n'))
  return lines.join('\n')
}

describe('md-notebook/Preview append index', () => {
  it('inserts at the trailing newline rather than past it', async () => {
    // The bug: `split('\n')` on 'Hello\n' is ['Hello', ''] (length 2), so the
    // append region started at 2 — one past the phantom — and 'World' landed
    // after it as 'Hello\n\nWorld'.
    const onStartEdit = renderPreview('Hello\n')
    await clickAppend()
    expect(onStartEdit).toHaveBeenCalledWith(1, 0)
    // Inserting AT the phantom's slot pushes it down, so the file keeps its
    // final newline instead of the append swallowing it.
    expect(commitAppend('Hello\n', 1, 0, 'World')).toBe('Hello\nWorld\n')
    // What the old index produced, for contrast.
    expect(commitAppend('Hello\n', 2, 1, 'World')).toBe('Hello\n\nWorld')
  })

  it('appends to a brand-new empty note without a leading blank line', async () => {
    // ''.split('\n') is [''] — one phantom, zero real lines.
    const onStartEdit = renderPreview('')
    await clickAppend()
    expect(onStartEdit).toHaveBeenCalledWith(0, -1)
    expect(commitAppend('', 0, -1, 'Hello')).toBe('Hello\n')
    expect(commitAppend('', 1, 0, 'Hello')).toBe('\nHello')
  })

  it('leaves a body with no trailing newline where it was', async () => {
    const onStartEdit = renderPreview('Hello')
    await clickAppend()
    expect(onStartEdit).toHaveBeenCalledWith(1, 0)
    expect(commitAppend('Hello', 1, 0, 'World')).toBe('Hello\nWorld')
  })

  it('keeps a genuine trailing blank line, dropping only the phantom', async () => {
    // 'Hello\n\n' splits to ['Hello', '', ''] — the first empty string is a real
    // blank line the user typed, so only the last one goes.
    const onStartEdit = renderPreview('Hello\n\n')
    await clickAppend()
    expect(onStartEdit).toHaveBeenCalledWith(2, 1)
    expect(commitAppend('Hello\n\n', 2, 1, 'World')).toBe('Hello\n\nWorld\n')
  })

  it('counts multi-line bodies from the last real line', async () => {
    const onStartEdit = renderPreview('# Title\n\nBody text\n')
    await clickAppend()
    expect(onStartEdit).toHaveBeenCalledWith(3, 2)
    expect(commitAppend('# Title\n\nBody text\n', 3, 2, 'More')).toBe(
      '# Title\n\nBody text\nMore\n',
    )
  })

  it('stays body-relative when the note carries frontmatter', async () => {
    // Preview strips frontmatter, so the index is body-relative and
    // `fmOffset()` re-aligns it with the file at commit time.
    const onStartEdit = renderPreview('---\ntitle: T\n---\nHello\n')
    await clickAppend()
    expect(onStartEdit).toHaveBeenCalledWith(1, 0)
  })

  it('mounts exactly one editor when the append region is active', () => {
    // The phantom line used to render its own block at the same index the append
    // region claimed, so an active append range matched both and two editors
    // mounted at once.
    renderPreview('Hello\n', { start: 1, end: 0 })
    expect(screen.getAllByRole('textbox')).toHaveLength(1)
  })

  it('does not render a block for the phantom trailing line', () => {
    // One block for 'Hello', one for the append region — not three.
    renderPreview('Hello\n')
    expect(screen.getAllByRole('button')).toHaveLength(2)
  })
})
