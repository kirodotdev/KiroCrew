/**
 * GFM task lists must render in block flow — never as flex rows.
 *
 * Regression locked here: task <li>s previously used `flex items-start`,
 * which broke two ways:
 *  1. an item containing a NESTED task list (the shape of every spec
 *     tasks.md: `- [ ] 1. Parent` with `- [ ] 1.1 …` children) laid the
 *     nested <ul> out BESIDE the parent's text instead of below it;
 *  2. any item long enough to wrap turned each inline chunk (text node,
 *     code chip) into its own flex item — text stacked vertically inside one
 *     chunk while siblings floated beside it — and flex min-width:auto
 *     prevented wrapping entirely, forcing horizontal scroll on the panel.
 *
 * Contract: task items are block-flow with a hanging indent (checkbox on the
 * first line, wrapped lines indented under the text), wrapping enabled, and
 * nested lists as block children below the text.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

const NESTED = [
  '- [ ] 1. Add TeamsConfig and credential wiring',
  '  - [ ] 1.1 Add `TeamsConfig` dataclass in `src/kiro_crew/config/loader.py`',
  '  - [ ] 1.2 Add `teams: TeamsConfig` to `KiroCrewConfig`',
].join('\n')

const FLAT = ['- [ ] first thing', '- [x] second thing'].join('\n')

// A LOOSE task list (blank line between items) — remark-rehype wraps each
// item's content in <p> and adds a SECOND paragraph under the first item.
// text-indent is inherited, so without a reset that second <p> would inherit
// the -1.25rem hanging indent and jut left into the checkbox gutter.
const LOOSE = [
  '- [ ] first item',
  '',
  '  a description paragraph under the first item',
  '',
  '- [x] second item',
].join('\n')

function taskItems(container: HTMLElement): HTMLLIElement[] {
  // The renderer replaces the remark className, so identify task items
  // structurally: an <li> whose direct children include a checkbox.
  return Array.from(container.querySelectorAll('li')).filter(
    (li) => li.querySelector(':scope > input[type=checkbox]') !== null,
  )
}

describe('MarkdownRenderer GFM task lists', () => {
  it('renders task items in block flow (no flex), with wrapping enabled', () => {
    const { container } = render(<MarkdownRenderer content={FLAT} />)
    const items = taskItems(container)
    expect(items.length).toBe(2)
    for (const li of items) {
      expect(li.className).not.toContain('flex')
      expect(li.className).toContain('break-words')
    }
  })

  it('keeps the hanging indent pair so the checkbox aligns with the first line', () => {
    const { container } = render(<MarkdownRenderer content={FLAT} />)
    for (const li of taskItems(container)) {
      expect(li.className).toContain('pl-5')
      expect(li.className).toContain('-indent-5')
    }
  })

  it('renders a nested task list as a block child below its parent text', () => {
    const { container } = render(<MarkdownRenderer content={NESTED} />)
    const items = taskItems(container)
    expect(items.length).toBe(3)
    const parent = items[0]
    const checkbox = parent.querySelector(':scope > input[type=checkbox]')
    expect(checkbox).not.toBeNull()
    const nested = parent.querySelector(':scope > ul')
    expect(nested).not.toBeNull()
    // Block flow on the parent: no flex class anywhere in its list.
    expect(parent.className).not.toContain('flex')
    // Nested list resets the hanging indent so children align normally.
    expect(parent.className).toContain('[&>ul]:indent-0')
  })

  it('resets the inherited hanging indent on trailing block children of a loose item', () => {
    const { container } = render(<MarkdownRenderer content={LOOSE} />)
    // In a loose list the checkbox is nested inside the first <p>, so find
    // task items by ANY descendant checkbox rather than a direct child.
    const items = Array.from(container.querySelectorAll('li')).filter(
      (li) => li.querySelector('input[type=checkbox]') !== null,
    )
    expect(items.length).toBe(2)
    const first = items[0]
    // Confirm this really is the loose shape: the checkbox lives inside a <p>,
    // and there is a SECOND paragraph that would otherwise inherit the indent.
    expect(first.querySelector(':scope > p > input[type=checkbox]')).not.toBeNull()
    expect(first.querySelectorAll(':scope > p').length).toBeGreaterThanOrEqual(2)
    // The reset that keeps trailing paragraphs out of the checkbox gutter.
    expect(first.className).toContain('[&>p:not(:first-child)]:indent-0')
    // Checkbox styling reaches the nested (non-direct-child) checkbox.
    expect(first.className).toContain('[&_input[type=checkbox]]:mr-1.5')
  })
})
