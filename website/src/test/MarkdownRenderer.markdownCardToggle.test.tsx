// @vitest-environment happy-dom
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * #9196 — a ```markdown content card in the chat transcript gets a Formatted |
 * Raw view toggle, matching the segmented control tool detail cards carry.
 *
 * The card is only the CHAT TRANSCRIPT surface, gated by the `mdCardToggle`
 * prop (the same shape as `collapseDiffs`). Everywhere else the renderer is
 * used — artifacts, specs, knowledge docs — a ```markdown fence stays verbatim
 * source, because there the fence IS the source being shown.
 *
 * Both views stay MOUNTED and the inactive one is hidden with `hidden`, so the
 * Raw scratch editor's unsaved edits survive a switch to Formatted. So these
 * assertions test VISIBILITY (the `hidden` class on each view's wrapper), not
 * DOM presence: the rendered <h1> is always in the tree.
 */

const MD_FENCE = '```markdown\n# Heading One\n\nsome **bold** prose\n```'

/** True when the view wrapper holding the rendered <h1> carries `hidden`. The
 *  two view wrappers are the direct children of the card after the toggle row. */
function formattedHidden(container: HTMLElement): boolean {
  const card = container.querySelector('div.my-2')!
  const h1 = card.querySelector('h1')
  expect(h1).not.toBeNull()
  // Walk up from the <h1> to the direct child of the card: that is the wrapper.
  let node: HTMLElement = h1 as unknown as HTMLElement
  while (node.parentElement && node.parentElement !== card) node = node.parentElement
  return node.classList.contains('hidden')
}

describe('#9196 markdown content card Formatted/Raw toggle', () => {
  beforeEach(() => { localStorage.clear() })
  afterEach(() => { localStorage.clear(); vi.restoreAllMocks() })

  it('renders formatted by default (a real <h1> is shown) with a Formatted|Raw control', () => {
    const { container, getByText } = render(<MarkdownRenderer content={MD_FENCE} mdCardToggle />)
    const h1 = container.querySelector('h1')
    expect(h1).not.toBeNull()
    expect(h1!.textContent).toContain('Heading One')
    expect(formattedHidden(container)).toBe(false) // Formatted view visible
    expect(getByText('Formatted')).toBeTruthy()
    expect(getByText('Raw')).toBeTruthy()
  })

  it('hides the formatted view and shows verbatim source on Raw, and restores it on Formatted', () => {
    const { container, getByText } = render(<MarkdownRenderer content={MD_FENCE} mdCardToggle />)
    expect(formattedHidden(container)).toBe(false)
    fireEvent.click(getByText('Raw'))
    // Formatted view is now hidden; the verbatim "# Heading One" source is shown.
    expect(formattedHidden(container)).toBe(true)
    expect(container.textContent).toContain('# Heading One')
    fireEvent.click(getByText('Formatted'))
    expect(formattedHidden(container)).toBe(false)
  })

  it('keeps the Raw view MOUNTED across a switch to Formatted so its editor state is not torn down (#9196 GPT fix)', () => {
    const { container, getByText } = render(<MarkdownRenderer content={MD_FENCE} mdCardToggle />)
    const card = container.querySelector('div.my-2')!
    // The Raw view wrapper is the card child that is NOT the one holding the <h1>.
    const wrappers = Array.from(card.children).filter(c => c.tagName === 'DIV') as HTMLElement[]
    const rawWrapper = wrappers.find(w => !w.querySelector('h1'))!
    expect(rawWrapper).toBeTruthy()
    const rawNodeBefore = rawWrapper.firstElementChild
    expect(rawNodeBefore).not.toBeNull() // EditableCodeBlock is mounted even while hidden
    // Toggle Raw -> Formatted -> Raw: the SAME node instance must persist (React
    // did not unmount it), which is what preserves any open scratch editor.
    fireEvent.click(getByText('Raw'))
    fireEvent.click(getByText('Formatted'))
    const rawWrapperAfter = Array.from(card.children).filter(c => c.tagName === 'DIV').find(w => !(w as HTMLElement).querySelector('h1')) as HTMLElement
    expect(rawWrapperAfter.firstElementChild).toBe(rawNodeBefore)
  })

  it('leaves a ```markdown fence verbatim (no toggle) when mdCardToggle is off', () => {
    const { container, queryByText } = render(<MarkdownRenderer content={MD_FENCE} />)
    expect(container.querySelector('h1')).toBeNull()
    expect(container.textContent).toContain('# Heading One')
    expect(queryByText('Formatted')).toBeNull()
    expect(queryByText('Raw')).toBeNull()
  })

  it('does not add a toggle to a non-markdown code fence even in the transcript', () => {
    const jsFence = '```js\nconst x = 1\n```'
    const { queryByText } = render(<MarkdownRenderer content={jsFence} mdCardToggle />)
    expect(queryByText('Formatted')).toBeNull()
    expect(queryByText('Raw')).toBeNull()
  })
})
