// @vitest-environment jsdom
import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import MarkdownRenderer, { TaskToggleCtx } from '../components/MarkdownRenderer'

// Ledger v2 view mode: the panel renders ledger markdown through the shared
// MarkdownRenderer with sourcePos, and TaskToggleCtx makes GFM task-list
// checkboxes interactive — a click reports the ABSOLUTE 0-based source line
// (data-sourcepos line + data-block-start − 2). Everywhere else (no provider)
// checkboxes stay disabled exactly as the sanitizer has always emitted them.

const DOC = [
  '# Ledger title',   // line 0
  '',                 // line 1
  'Some **intro** text.', // line 2
  '',                 // line 3
  '- [ ] first task', // line 4
  '- [x] second task, done', // line 5
  '',                 // line 6
  '## Section two',   // line 7
  '',                 // line 8
  '- [ ] third task with a [link](https://example.com)', // line 9
].join('\n')

function renderWithToggle(content: string, onToggle: (i: number) => void) {
  return render(
    <TaskToggleCtx.Provider value={onToggle}>
      <MarkdownRenderer content={content} sourcePos />
    </TaskToggleCtx.Provider>
  )
}

describe('interactive task checkboxes (TaskToggleCtx)', () => {
  it('renders full markdown: headings, bold, links, and enabled checkboxes', () => {
    const { container } = renderWithToggle(DOC, vi.fn())
    expect(container.querySelector('h1')?.textContent).toBe('Ledger title')
    expect(container.querySelector('h2')?.textContent).toBe('Section two')
    expect(container.querySelector('strong')?.textContent).toBe('intro')
    expect(container.querySelector('a')?.getAttribute('href')).toBe('https://example.com')
    const boxes = container.querySelectorAll('input[type="checkbox"]')
    expect(boxes.length).toBe(3)
    boxes.forEach(b => expect((b as HTMLInputElement).disabled).toBe(false))
    expect((boxes[1] as HTMLInputElement).checked).toBe(true)
  })

  it('reports the absolute 0-based source line on click', () => {
    const onToggle = vi.fn()
    const { container } = renderWithToggle(DOC, onToggle)
    const boxes = container.querySelectorAll('input[type="checkbox"]')
    fireEvent.click(boxes[0])
    expect(onToggle).toHaveBeenCalledWith(4)
    fireEvent.click(boxes[2])
    expect(onToggle).toHaveBeenCalledWith(9)
    // Sanity: the reported indexes point at the actual checkbox lines
    const lines = DOC.split('\n')
    expect(lines[4]).toBe('- [ ] first task')
    expect(lines[9]).toContain('third task')
  })

  it('maps lines correctly for nested checkboxes', () => {
    const nested = '- [ ] parent\n    - [ ] child'
    const onToggle = vi.fn()
    const { container } = renderWithToggle(nested, onToggle)
    const boxes = container.querySelectorAll('input[type="checkbox"]')
    expect(boxes.length).toBe(2)
    fireEvent.click(boxes[1])
    expect(onToggle).toHaveBeenCalledWith(1)
  })

  it('keeps checkboxes disabled without a provider (chat rendering unchanged)', () => {
    const { container } = render(<MarkdownRenderer content={'- [ ] task'} sourcePos />)
    const box = container.querySelector('input[type="checkbox"]') as HTMLInputElement
    expect(box).toBeTruthy()
    expect(box.disabled).toBe(true)
  })
})
