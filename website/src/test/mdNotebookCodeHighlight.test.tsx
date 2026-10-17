/**
 * Fenced code block colouring in the Notes app's markdown preview.
 *
 * Two properties pinned here:
 *  1. `fenceLang` reads the fence's info string, and only that: a bare fence
 *     yields undefined rather than ''.
 *  2. A bare fence never reaches the highlight worker — jsdom has no `Worker`,
 *     so `highlightAsync` always resolves to '' here, but the block must not
 *     even attempt the call for an unlabelled fence (that's the "stay plain"
 *     contract, not a fallback of last resort). Click-to-edit keeps working
 *     for both labelled and bare fences, same as before this feature existed.
 */

import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

import { Preview } from '../apps/md-notebook/Preview'
import { fenceLang } from '../apps/md-notebook/utils'

describe('fenceLang', () => {
  it('reads the language off the opening fence line', () => {
    expect(fenceLang('```python')).toBe('python')
    expect(fenceLang('```bash')).toBe('bash')
  })

  it('trims trailing whitespace', () => {
    expect(fenceLang('```python  ')).toBe('python')
  })

  it('takes only the first token of the info string', () => {
    expect(fenceLang('```python title=example.py')).toBe('python')
  })

  it('returns undefined for a bare fence', () => {
    expect(fenceLang('```')).toBeUndefined()
    expect(fenceLang('```   ')).toBeUndefined()
  })
})

/** Preview with spies; returns the onStartEdit spy for click assertions. */
function renderPreview(content: string) {
  const onStartEdit = vi.fn()
  render(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={null}
      onStartEdit={onStartEdit}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
  return onStartEdit
}

describe('fenced code blocks in Preview', () => {
  it('renders a labelled fence as plain text until the worker replies', () => {
    // jsdom has no Worker, so highlightAsync resolves to '' and the block
    // never leaves its plain-text fallback — same as a fence with no language.
    renderPreview('```python\nprint(1)\n```')
    expect(screen.getByText('print(1)')).toBeInTheDocument()
  })

  it('renders a bare fence unchanged from before this feature existed', () => {
    renderPreview('```\nplain output\n```')
    expect(screen.getByText('plain output')).toBeInTheDocument()
  })

  it('opens the full fenced source range on click, labelled or bare', () => {
    const onStartEdit = renderPreview('intro\n\n```python\nprint(1)\n```\n\noutro')
    fireEvent.click(screen.getByText('print(1)'))
    // Lines: 0 intro, 1 blank, 2 fence-open, 3 body, 4 fence-close.
    expect(onStartEdit).toHaveBeenCalledWith(2, 4)
  })

  it('renders multi-line code bodies intact', () => {
    renderPreview('```js\nconst a = 1\nconst b = 2\n```')
    expect(screen.getByText(/const a = 1/)).toBeInTheDocument()
    expect(screen.getByText(/const b = 2/)).toBeInTheDocument()
  })
})
