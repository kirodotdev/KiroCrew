import { it, expect } from 'vitest'
import { render, fireEvent, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'

// The scope line under the card's title says what the list is, so the card
// does not read as "the link you clicked".
const host = 'reviews.example-sink.net'
const PH = `[REDACTED: suspicious URL to ${host}]`
const records = [1, 2].map(i => ({ domain: host, rule: 'exfil_query_length', path: `/p${i}`, query_chars: 290, url: `https://${host}/p${i}?q=x`, url_withheld: null }))
const content = records.map((_, i) => `Link ${i + 1}: ${PH}`).join(' ')

it('the card says the list is every blocked link to the host in this message', () => {
  const { getAllByTestId, getByTestId } = render(<MarkdownRenderer content={content} blockedLinks={records} slotKey="s1" />)
  fireEvent.click(getAllByTestId('blocked-link-inspect')[1])
  const card = getByTestId('blocked-link-card')
  const scope = within(card).getByTestId('blocked-link-card-scope')
  expect(scope.textContent).toBe('Every blocked link to this host in this message')
  expect(scope.className).toContain('text-muted')
  // Directly under the title, before the first entry.
  expect(card.querySelector('h4')?.nextElementSibling).toBe(scope)
})
