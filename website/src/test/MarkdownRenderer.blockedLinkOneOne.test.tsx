import { it, expect } from 'vitest'
import { render, fireEvent, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import fixture from './fixtures/blockedLinkOneOne.json'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'
import { i18nT } from '../i18n/t'

// Two links on one host and one record: a report.csv link the decision passed
// beside a same-host URL carrying a key, which it blocked. The text keeps both
// addresses as written. Only the recorded link draws the chip; the other
// renders live. The chip names the host alone, and the card lists the record
// under its own address and never presents it as the report.csv link.
it('one record of two same-host links: only its link draws the chip, and the card shows its own address', () => {
  const { getAllByTestId, getByTestId, container } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  expect(getAllByTestId('blocked-link-inspect')).toHaveLength(1)
  const host = fixture.blocked_links[0].domain
  expect(container.querySelector(`a[href^="https://${host}/report.csv"]`)).toBeTruthy()
  expect(container.querySelector(`a[href="${fixture.blocked_links[0].url}"]`)).toBeNull()
  fireEvent.click(getAllByTestId('blocked-link-inspect')[0])
  expectCardListsHostRecords(getByTestId('blocked-link-card'), fixture.blocked_links, host)
  expect(getByTestId('blocked-link-card').textContent).not.toContain('report.csv')
})

it('chip label does not name a record as the removed link', () => {
  const { getAllByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  const chips = getAllByTestId('blocked-link-chip')
  expect(chips).toHaveLength(1)
  const rec = fixture.blocked_links[0]
  expect(within(chips[0]).getByTestId('blocked-link-target').textContent).toBe(rec.domain)
  expect(chips[0].textContent).not.toContain('/other-a')
  expect(within(chips[0]).queryByTestId('blocked-link-query')).toBeNull()
})

it('the lone record is listed under its own address, with no claim that it is the clicked link', () => {
  // The card frames one record the way it frames several: a blocked link to
  // this host, under its own address, below a title that never says "this link".
  const { getByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  fireEvent.click(getByTestId('blocked-link-inspect'))
  const card = getByTestId('blocked-link-card')
  const rec = fixture.blocked_links[0]
  expect(within(card).getByTestId('blocked-link-entry-target').textContent).toBe(`${rec.domain}/other-a`)
  expect(card.textContent).toContain(i18nT('components.redaction.link_title_host'))
  expect(card.textContent).not.toMatch(/this link/i)
  // Opening the address would send its key, so the card offers no way to open it.
  expect(within(card).queryByTestId('blocked-link-review')).toBeNull()
  expect(within(card).queryByTestId('blocked-link-open-once')).toBeNull()
  expect(within(card).getByTestId('blocked-link-withheld')).toBeTruthy()
})

it('the raw view shows every address as written: the reply is never rewritten for a blocked link', () => {
  const { container } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" rawMode />)
  expect(container.textContent).toContain(fixture.blocked_links[0].url)
  expect(container.textContent).not.toContain('[REDACTED: suspicious URL')
})
