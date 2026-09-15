// @vitest-environment happy-dom
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../src/components/MarkdownRenderer'
import { resolveSourcePos } from '../src/components/MarkdownPanel'

describe('MarkdownRenderer sourcePos with block splits', () => {
  it('resolves a selection in a markdown block AFTER a fenced code block', () => {
    // useBlockAssembler splits this into [md "before", code "x = 1", md "after"].
    // The 2nd md block's internal line 1 = source line 7.
    const content = 'before\n\n```js\nx = 1\n```\n\nafter'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const ps = container.querySelectorAll('p')
    // 2nd <p> is "after" — should resolve to line 7, col 1.
    const text = ps[1].firstChild as Text
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 5)
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 7, column: 1 })
  })

  it('disambiguates duplicate word across blocks (selects 2nd "alpha" after code fence)', () => {
    const content = 'alpha\n\n```\nnoise\n```\n\nalpha'
    const { container } = render(<MarkdownRenderer content={content} sourcePos />)
    const root = container.querySelector('.group') as HTMLElement
    const ps = container.querySelectorAll('p')
    const text = ps[1].firstChild as Text
    const range = document.createRange()
    range.setStart(text, 0); range.setEnd(text, 5)
    // The 2nd "alpha" is on source line 7, not line 1.
    expect(resolveSourcePos(range, root, content)).toEqual({ line: 7, column: 1 })
  })
})
