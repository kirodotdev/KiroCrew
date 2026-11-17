import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * A closed ```markdown fence is offered a Formatted view in the chat. The reply's
 * blocked-link decisions reach that view too: a link the server blocked is its
 * chip there, never a live anchor, the same as in the reply's own text.
 */

const URL = 'https://reports.example.org/?data=' + 'a'.repeat(220)
const record = { url: URL, domain: 'reports.example.org', rule: 'exfil_query_length', query_chars: 225, withheld: null }
const content = ['Here is the report:', '', '```markdown', `[Report](${URL})`, '```'].join('\n')

afterEach(() => cleanup())

describe('a blocked link inside a formatted markdown card', () => {
  it('is drawn as its chip, not as a live anchor', () => {
    const { container } = render(
      <MarkdownRenderer content={content} blockedLinks={[record]} slotKey="s1" mdCardToggle />,
    )
    const live = Array.from(container.querySelectorAll('a')).filter(a => a.getAttribute('href') === URL)
    expect(live).toEqual([])
    expect(container.querySelector('[data-testid="blocked-link-chip"]')).not.toBeNull()
  })
  it('is drawn as its chip when it is a top-level raw anchor or image', () => {
    const { container } = render(
      <MarkdownRenderer content={`<a href="${URL}">\nReport</a>\n\n<img src="${URL}">`} blockedLinks={[record]} slotKey="s1" />,
    )
    const live = Array.from(container.querySelectorAll('a')).filter(a => a.getAttribute('href') === URL)
    expect(live).toEqual([])
    const loads = Array.from(container.querySelectorAll('img')).filter(img => img.getAttribute('src') === URL)
    expect(loads).toEqual([])
  })
})
