import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * `data-message-*` is the transcript's own namespace: UserMessage marks its
 * action strip `data-message-actions`, its pencil `data-message-edit` and its
 * editing render `data-message-editing`. usePinnedPrompt measures the strip's
 * rect and reads the editing marker off the row it is about to hide, and
 * index.css re-shows `[data-message-actions]` inside that hidden row. The
 * sanitizer admits every other `data-*` (inert metadata), so without a
 * reservation a prompt body — typed, or relayed from a connected channel —
 * could mint one of these hooks inside its own bubble: a forged editing marker
 * dropped the banner for that prompt's whole scroll region, and a forged strip,
 * earlier in document order than the real one, was measured in its place and
 * painted visible inside the otherwise-hidden row. Reserved by NORMALIZED key,
 * the way `data-fenced` is, because the HTML parser lowercases attribute names
 * and hast camelCases `data-*`.
 */
describe('MarkdownRenderer reserves the transcript data-message-* hooks', () => {
  it('drops content-minted data-message-* attributes in every spelling', () => {
    const md = [
      '<span data-message-editing>x</span>',
      '<div data-message-actions>y</div>',
      '<b data-Message-Edit>z</b>',
    ].join(' ')
    const { container } = render(<MarkdownRenderer content={md} />)
    // The elements themselves are allowlisted and still render.
    expect(container.textContent).toContain('x')
    expect(container.textContent).toContain('y')
    expect(container.textContent).toContain('z')
    expect(container.querySelector('[data-message-editing]')).toBeNull()
    expect(container.querySelector('[data-message-actions]')).toBeNull()
    expect(container.querySelector('[data-message-edit]')).toBeNull()
    // Nor any other casing or dash placement the serializer could hand back
    // as one of the hooks.
    const leaked = Array.from(container.querySelectorAll('*')).flatMap(el =>
      Array.from(el.attributes).map(a => a.name).filter(n => n.toLowerCase().replace(/-/g, '').startsWith('datamessage')))
    expect(leaked).toEqual([])
  })

  it('still admits an ordinary data-* attribute', () => {
    const { container } = render(<MarkdownRenderer content={'<span data-foo="bar">kept</span>'} />)
    const span = container.querySelector('span[data-foo]')
    expect(span).not.toBeNull()
    expect(span!.getAttribute('data-foo')).toBe('bar')
  })
})
