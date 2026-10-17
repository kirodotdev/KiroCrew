// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

// A fenced code block SPLITS a message into independent markdown documents
// (useBlockAssembler), so a numbered set of steps interleaved with shell
// snippets arrives as several <ol>s — the second one legitimately starting at
// 2, the third at 3. The `ol` override used to drop the hast `start` property
// while building its className, which renumbered every one of those back to 1:
// four numbered steps all rendered as "1.".
//
// `start` is already admitted by the sanitize schema, so the override was the
// only thing losing it. These tests pin the forward, and pin that a list that
// really does begin at 1 emits no redundant attribute.

describe('MarkdownRenderer ordered lists — start is preserved', () => {
  it('keeps the authored start on a list that does not begin at 1', () => {
    const { container } = render(<MarkdownRenderer content={'2. second step\n3. third step'} />)
    const ol = container.querySelector('ol')
    expect(ol).not.toBeNull()
    expect(ol!.getAttribute('start')).toBe('2')
  })

  it('emits no start attribute for a list beginning at 1', () => {
    const { container } = render(<MarkdownRenderer content={'1. first step\n2. second step'} />)
    const ol = container.querySelector('ol')
    expect(ol).not.toBeNull()
    expect(ol!.hasAttribute('start')).toBe(false)
  })

  it('renumbers each chunk of a fence-split step list from its own number', () => {
    // The shape from the bug report: steps separated by ```bash blocks.
    const content = [
      '1. back it up:',
      '```bash',
      'cp a.json a.json.bak',
      '```',
      '2. edit it:',
      '```bash',
      'vi a.json',
      '```',
      '3. restart:',
    ].join('\n')
    const { container } = render(<MarkdownRenderer content={content} />)
    const starts = Array.from(container.querySelectorAll('ol')).map(ol => ol.getAttribute('start'))
    // First list starts at 1 (no attribute); the two after a code block carry
    // their real numbers instead of restarting.
    expect(starts).toEqual([null, '2', '3'])
  })

  it('keeps the list markers visible for a task list without a start', () => {
    // The task-list branch takes a different className; it must not regress.
    const { container } = render(<MarkdownRenderer content={'3. plain\n4. items'} />)
    const ol = container.querySelector('ol')
    expect(ol!.className).toContain('list-decimal')
    expect(ol!.getAttribute('start')).toBe('3')
  })

  it('gives a raw-HTML typed list a real marker instead of none', () => {
    // Dropping `list-decimal` is not enough: Tailwind's preflight resets
    // list-style to none and beats the attribute's presentational hint, so a
    // typed list needs the style restated or it renders with NO marker.
    const { container } = render(<MarkdownRenderer content={'<ol type="A"><li>alpha</li><li>beta</li></ol>'} />)
    const ol = container.querySelector('ol') as HTMLOListElement
    expect(ol.getAttribute('type')).toBe('A')
    expect(ol.className).not.toContain('list-decimal')
    expect(ol.style.listStyleType).toBe('upper-alpha')
    expect(ol.className).toContain('pl-8')
  })

  it('still emits list-decimal for an explicit numeric type', () => {
    const { container } = render(<MarkdownRenderer content={'<ol type="1"><li>one</li></ol>'} />)
    const ol = container.querySelector('ol') as HTMLOListElement
    expect(ol.className).toContain('list-decimal')
    expect(ol.style.listStyleType).toBe('')
  })

  it('falls back to the decimal class for an unrecognized type', () => {
    const { container } = render(<MarkdownRenderer content={'<ol type="zz"><li>one</li></ol>'} />)
    const ol = container.querySelector('ol') as HTMLOListElement
    expect(ol.className).toContain('list-decimal')
    expect(ol.style.listStyleType).toBe('')
  })
})
