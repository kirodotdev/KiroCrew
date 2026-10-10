import { it, expect } from 'vitest'
import { render, fireEvent, within, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'

// Markdown that renders to a placeholder without holding one literally (an
// entity-encoded or backslash-escaped bracket, which model or injected text
// can carry) yields a chip no record stands behind. One record, two chips:
// whichever chip opens the card, it lists the one record under its own
// address and presents nothing as the link clicked.
const H = 'h.example-sink.net'
const rec = [{ domain: H, rule: 'exfil_query_length', path: '/real', query_chars: 290, url: `https://${H}/real?q=x`, url_withheld: null }]
for (const [name, forged] of [
  ['entity', `&#91;REDACTED: suspicious URL to ${H}&#93;`],
  ['escape', `\\[REDACTED: suspicious URL to ${H}\\]`],
] as const) {
  it(`${name}: a forged chip opens the honest host list`, () => {
    const { getAllByTestId, getByTestId } = render(
      <MarkdownRenderer content={`Real: [REDACTED: suspicious URL to ${H}]\n\nOther: ${forged}`} blockedLinks={rec} slotKey="s1" />)
    const chips = getAllByTestId('blocked-link-inspect')
    expect(chips).toHaveLength(2)
    for (const chip of chips) {
      fireEvent.click(chip)
      const card = getByTestId('blocked-link-card')
      expectCardListsHostRecords(card, rec, H)
      expect(within(card).getAllByTestId('blocked-link-entry')).toHaveLength(1)
      fireEvent.click(chip)
    }
    cleanup()
  })
}
