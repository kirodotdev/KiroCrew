import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import { ToolInputText } from '../components/ToolInputText'

describe('ToolInputText', () => {
  it('renders plain text when no diff markers present', () => {
    const { container } = render(<ToolInputText text="just some text" />)
    expect(container.textContent).toBe('just some text')
    expect(container.querySelectorAll('.bg-diff-add')).toHaveLength(0)
  })

  it('colors added lines green', () => {
    const { container } = render(<ToolInputText text={"@@ -1,1 +1,1 @@\n+added line"} />)
    const div = container.querySelector('.bg-diff-add')
    expect(div).not.toBeNull()
    expect(div!.textContent).toContain('added line')
  })

  it('colors removed lines red', () => {
    const { container } = render(<ToolInputText text={"@@ -1,1 +1,1 @@\n-removed line"} />)
    const div = container.querySelector('.bg-diff-del')
    expect(div).not.toBeNull()
    expect(div!.textContent).toContain('removed line')
  })

  it('strips --- and +++ path headers', () => {
    const text = "--- /home/user/file.ts\n+++ /home/user/file.ts\n@@ -1,2 +1,2 @@\n-old\n+new"
    const { container } = render(<ToolInputText text={text} />)
    expect(container.textContent).not.toContain('---')
    expect(container.textContent).not.toContain('+++')
    expect(container.textContent).toContain('old')
    expect(container.textContent).toContain('new')
  })

  it('renders hunk headers with hunk styling', () => {
    const text = "@@ -1,3 +1,3 @@\n context\n-old\n+new"
    const { container } = render(<ToolInputText text={text} />)
    expect(container.querySelector('.bg-diff-hunk')).not.toBeNull()
  })

  it('renders multi-line diffs with correct classes', () => {
    const text = "@@ -1,2 +1,2 @@\n-old line 1\n-old line 2\n+new line 1\n+new line 2\n context line"
    const { container } = render(<ToolInputText text={text} />)
    expect(container.querySelectorAll('.bg-diff-del')).toHaveLength(2)
    expect(container.querySelectorAll('.bg-diff-add')).toHaveLength(2)
  })

  it('highlights JSON keys and values', () => {
    const text = '{"name": "test", "count": 42, "active": true}'
    const { container } = render(<ToolInputText text={text} />)
    const spans = container.querySelectorAll('span')
    expect(spans.length).toBeGreaterThan(1)
  })

  it('falls through to plain text when JSON regex finds no matches', () => {
    const text = '{not valid json'
    const { container } = render(<ToolInputText text={text} />)
    expect(container.textContent).toBe(text)
  })

  it('highlights truncated JSON with valid regex matches', () => {
    const text = '{"key": "val"'
    const { container } = render(<ToolInputText text={text} />)
    const spans = container.querySelectorAll('span')
    expect(spans.length).toBeGreaterThan(1)
  })

  it('skips JSON highlighting for very large text', () => {
    const text = '{"key": "' + 'x'.repeat(60000) + '"}'
    const { container } = render(<ToolInputText text={text} />)
    expect(container.textContent).toBe(text)
  })

  it('formatted mode (default) unescapes \\n inside JSON string values', () => {
    const { container } = render(<ToolInputText text={'{"command": "a\\nb"}'} />)
    expect(container.textContent).toContain('a\nb') // real newline
    expect(container.textContent).not.toContain('a\\nb') // no literal backslash-n
  })

  it('raw mode preserves \\n escapes verbatim', () => {
    const { container } = render(<ToolInputText text={'{"command": "a\\nb"}'} raw />)
    expect(container.textContent).toContain('a\\nb') // literal backslash-n kept
    expect(container.textContent).not.toContain('a\nb') // not turned into a newline
  })

  it('formatted mode preserves a genuine literal backslash-n (JSON \\\\n)', () => {
    // JSON "\\n" encodes a literal backslash + n, which must NOT become a newline.
    const { container } = render(<ToolInputText text={'{"command": "a\\\\nb"}'} />)
    expect(container.textContent).toContain('a\\nb') // still backslash-n
    expect(container.textContent).not.toContain('a\nb') // not a real newline
  })
})
