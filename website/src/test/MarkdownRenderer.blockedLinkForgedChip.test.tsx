import { it, expect } from 'vitest'
import { render, fireEvent, within, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'

// A chip stands only where a rendered link's address matches a record. Text
// shaped like a blocked-link placeholder (literal, entity-encoded or
// backslash-escaped, which model or injected text can carry) is prose and draws
// no chip, and a link one character away from the recorded address renders
// live. The one recorded link draws the one chip, whose card lists the record
// under its own address and presents nothing as the link clicked.
const H = 'h.example-sink.net'
const rec = [{ url: `https://${H}/real?q=x`, domain: H, rule: 'exfil_query_length', query_chars: 290, withheld: null }]
for (const [name, forged] of [
  ['literal', `[REDACTED: suspicious URL to ${H}]`],
  ['entity', `&#91;REDACTED: suspicious URL to ${H}&#93;`],
  ['escape', `\\[REDACTED: suspicious URL to ${H}\\]`],
] as const) {
  it(`${name}: placeholder-shaped text draws no chip, and only the recorded link does`, () => {
    const near = `https://${H}/reak?q=x`
    const { getAllByTestId, getByTestId, container } = render(
      <MarkdownRenderer content={`Real: ${rec[0].url}\n\nOther: ${forged}\n\nNear: ${near}`} blockedLinks={rec} slotKey="s1" />)
    const chips = getAllByTestId('blocked-link-chip')
    expect(chips).toHaveLength(1)
    expect(within(chips[0]).getByTestId('blocked-link-target').textContent).toBe(H)
    expect(within(chips[0]).queryByTestId('blocked-link-query')).toBeNull()
    expect(chips[0].textContent).not.toContain('/real')
    expect(container.querySelector(`a[href="${near}"]`)).toBeTruthy()
    fireEvent.click(getByTestId('blocked-link-inspect'))
    const card = getByTestId('blocked-link-card')
    expectCardListsHostRecords(card, rec, H)
    expect(within(card).getAllByTestId('blocked-link-entry')).toHaveLength(1)
    cleanup()
  })
}
