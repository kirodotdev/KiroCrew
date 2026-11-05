/**
 * Tests for `<details>` collapsible blocks in the Notes app.
 *
 * Two halves, like the tables feature: the parser deciding where the block
 * starts and ends (and refusing a run-away or non-block line), and the renderer
 * keeping the click-to-edit contract — a native disclosure whose source opens
 * whole, with every byte of content escaped rather than passed through as HTML.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { parseDetailsBlock } from '../apps/md-notebook/utils'
import { detailsSummaryFallbackLabel } from '../apps/md-notebook/labels'
import { Preview } from '../apps/md-notebook/Preview'

function renderPreview(content: string, onStartEdit = vi.fn()) {
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

describe('md-notebook/parseDetailsBlock', () => {
  it('reads a summary on its own line, a body, and reports the close line', () => {
    const d = parseDetailsBlock(
      ['<details>', '<summary>Answer</summary>', 'the body', '</details>'],
      0,
    )
    expect(d).not.toBeNull()
    expect(d!.summary).toBe('Answer')
    expect(d!.body).toEqual(['the body'])
    expect(d!.end).toBe(3)
  })

  it('reads a summary written inline on the open tag', () => {
    const d = parseDetailsBlock(['<details><summary>Q</summary>', 'a', '</details>'], 0)
    expect(d!.summary).toBe('Q')
    expect(d!.body).toEqual(['a'])
    expect(d!.end).toBe(2)
  })

  it('reads the whole block on a single line', () => {
    const d = parseDetailsBlock(['<details><summary>Q</summary>body</details>'], 0)
    expect(d!.summary).toBe('Q')
    expect(d!.body).toEqual(['body'])
    expect(d!.end).toBe(0)
  })

  it('keeps every body line between the summary and the close', () => {
    const d = parseDetailsBlock(
      ['<details>', '<summary>s</summary>', 'line one', 'line two', '</details>'],
      0,
    )
    expect(d!.body).toEqual(['line one', 'line two'])
    expect(d!.end).toBe(4)
  })

  it('leaves the summary empty when the author wrote none', () => {
    const d = parseDetailsBlock(['<details>', 'just a body', '</details>'], 0)
    expect(d!.summary).toBe('')
    expect(d!.body).toEqual(['just a body'])
  })

  it('tolerates a trailing attribute on the open tag', () => {
    const d = parseDetailsBlock(['<details open>', '<summary>s</summary>', 'b', '</details>'], 0)
    expect(d).not.toBeNull()
    expect(d!.summary).toBe('s')
  })

  it('returns null for a line that does not open a details block', () => {
    expect(parseDetailsBlock(['plain text', '</details>'], 0)).toBeNull()
  })

  it('returns null for an unclosed block so it falls through to editable text', () => {
    expect(parseDetailsBlock(['<details>', '<summary>s</summary>', 'no close here'], 0)).toBeNull()
  })

  it('closes on the first </details> and ignores a second one', () => {
    const d = parseDetailsBlock(
      ['<details>', '<summary>s</summary>', 'a', '</details>', 'after', '</details>'],
      0,
    )
    expect(d!.end).toBe(3)
    expect(d!.body).toEqual(['a'])
  })

  it('does not scan across a code fence to a fenced </details>', () => {
    // An unclosed <details> followed by a fenced example must NOT reach into the
    // fence and treat the example's own </details> as its close: that would eat
    // the fence opener and render the rest of the note as one run-away block.
    const d = parseDetailsBlock(
      ['<details> folds content', '', '```html', '<details>', '</details>', '```'],
      0,
    )
    expect(d).toBeNull()
  })

  it('closes at its own </details> after an inner fenced example', () => {
    // A WELL-FORMED disclosure whose body contains a fenced code example must
    // still close at its real </details> outside the fence — the </details>
    // inside the fence is example text and must not be read as the close.
    const d = parseDetailsBlock(
      ['<details>', '<summary>Example</summary>', '```html', '<details></details>', '```', '</details>'],
      0,
    )
    expect(d).not.toBeNull()
    expect(d!.summary).toBe('Example')
    expect(d!.body).toEqual(['```html', '<details></details>', '```'])
    expect(d!.end).toBe(5)
  })

  it('tolerates whitespace inside the closing tag', () => {
    const d = parseDetailsBlock(['<details>', 'body', '</details >'], 0)
    expect(d).not.toBeNull()
    expect(d!.body).toEqual(['body'])
    expect(d!.end).toBe(2)
  })

  it('bails out when non-whitespace text follows the close tag', () => {
    // The text after </details> has nowhere to render (skipTo swallows the
    // line), so the block must fall through to the generic path rather than
    // drop it silently.
    expect(parseDetailsBlock(['<details>', 'body', '</details> and more'], 0)).toBeNull()
  })

  it('keeps trailing whitespace after the close tag', () => {
    const d = parseDetailsBlock(['<details>', 'body', '</details>   '], 0)
    expect(d).not.toBeNull()
    expect(d!.body).toEqual(['body'])
  })
})

describe('md-notebook/Preview details', () => {
  it('renders a native disclosure, not the literal tags', () => {
    const { container } = render(
      <Preview
        content={['<details>', '<summary>Answer</summary>', 'the body', '</details>'].join('\n')}
        onToggleCheckbox={vi.fn()}
        editRange={null}
        onStartEdit={vi.fn()}
        onCommitEdit={vi.fn()}
        onCancelEdit={vi.fn()}
        onSplitEdit={vi.fn()}
      />,
    )
    const details = container.querySelector('details')
    expect(details).not.toBeNull()
    expect(details!.open).toBe(false)
    expect(screen.getByText('Answer').tagName).toBe('SUMMARY')
    expect(screen.getByText('the body')).toBeTruthy()
    expect(screen.queryByText(/<details>/)).toBeNull()
    expect(screen.queryByText(/<summary>/)).toBeNull()
  })

  it('escapes HTML in the body rather than passing a raw tag through', () => {
    const { container } = renderAndReturn(
      ['<details>', '<summary>s</summary>', '<script>alert(1)</script>', '</details>'].join('\n'),
    )
    // The literal angle-bracket text survives as text content; no <script>
    // element is ever created from the note's content.
    expect(container.querySelector('script')).toBeNull()
    expect(screen.getByText('<script>alert(1)</script>')).toBeTruthy()
  })

  it('shows the localized fallback when the author wrote no summary', () => {
    renderPreview(['<details>', 'body only', '</details>'].join('\n'))
    expect(screen.getByText(detailsSummaryFallbackLabel()).tagName).toBe('SUMMARY')
  })

  it('renders inline markup inside the summary and body', () => {
    renderPreview(
      ['<details>', '<summary>**bold**</summary>', 'a `code` span', '</details>'].join('\n'),
    )
    expect(screen.getByText('bold').tagName).toBe('STRONG')
    expect(screen.getByText('code').tagName).toBe('CODE')
  })

  it('opens the whole block source when the body is clicked, not one line', async () => {
    const onStartEdit = renderPreview(
      ['intro', '<details>', '<summary>s</summary>', 'body', '</details>'].join('\n'),
    )
    await userEvent.click(screen.getByText('body'))
    expect(onStartEdit).toHaveBeenCalledWith(1, 4)
  })

  it('does not open the editor when the summary is clicked (it only toggles)', async () => {
    const onStartEdit = renderPreview(
      ['<details>', '<summary>s</summary>', 'body', '</details>'].join('\n'),
    )
    await userEvent.click(screen.getByText('s'))
    expect(onStartEdit).not.toHaveBeenCalled()
  })

  it('leaves an unclosed block as editable literal text', () => {
    renderPreview(['<details>', '<summary>s</summary>', 'dangling'].join('\n'))
    expect(screen.queryByText('s', { selector: 'summary' })).toBeNull()
    expect(screen.getByText(/<details>/)).toBeTruthy()
  })

  it('keeps rendering the blocks that follow the details block', () => {
    renderPreview(
      ['<details>', '<summary>s</summary>', 'body', '</details>', '', '# After'].join('\n'),
    )
    expect(screen.getByRole('heading', { name: 'After' })).toBeTruthy()
  })

  it('renders a fenced code block inside the body as code, not flattened text', () => {
    const { container } = renderAndReturn(
      [
        '<details>',
        '<summary>Example</summary>',
        '```python',
        'def f(*args):',
        '    return 1',
        '```',
        '</details>',
      ].join('\n'),
    )
    // The code is a <pre> block, so indentation and the asterisks survive and
    // the ```python fence line is not shown as literal text.
    const pre = container.querySelector('pre')
    expect(pre).not.toBeNull()
    expect(pre!.textContent).toContain('def f(*args):')
    expect(pre!.textContent).toContain('    return 1')
    expect(screen.queryByText(/```python/)).toBeNull()
  })

  it('does not suppress the summary focus ring with an inline outline', () => {
    // The summary is the keyboard target that toggles the disclosure, so it must
    // keep the app's global :focus-visible ring (WCAG 2.4.7). An inline
    // `outline: none` would override it with no replacement.
    const { container } = renderAndReturn(
      ['<details>', '<summary>s</summary>', 'body', '</details>'].join('\n'),
    )
    const summary = container.querySelector('summary') as HTMLElement
    expect(summary).not.toBeNull()
    expect(summary.style.outline).toBe('')
  })

  it('leaves a later fenced <details> example as code when a stray tag precedes it', () => {
    // A half-typed <details> above a fenced example must not collapse the fence
    // and everything after it into one run-away code block.
    renderPreview(
      ['<details> folds content', '', '```html', '<details>', '</details>', '```', '', '# After'].join(
        '\n',
      ),
    )
    // The blocks after the fence still render as themselves.
    expect(screen.getByRole('heading', { name: 'After' })).toBeTruthy()
  })
})

function renderAndReturn(content: string) {
  return render(
    <Preview
      content={content}
      onToggleCheckbox={vi.fn()}
      editRange={null}
      onStartEdit={vi.fn()}
      onCommitEdit={vi.fn()}
      onCancelEdit={vi.fn()}
      onSplitEdit={vi.fn()}
    />,
  )
}
