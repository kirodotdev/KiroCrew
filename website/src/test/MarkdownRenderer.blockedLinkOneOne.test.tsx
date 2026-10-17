import { it, expect } from 'vitest'
import { render, fireEvent, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import fixture from './fixtures/blockedLinkOneOne.json'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'
import { i18nT } from '../i18n/t'

// Backend output of TestBlockedLinkCardRecords._one_presigned_one_key_split_flush
// (test/test_chat_runner_coverage.py), byte for byte: ONE presigned S3 link
// split at &X-Amz-Date (a placeholder with no record of its own) beside ONE
// same-host URL whose key a delta redacted (a record the text shows no
// placeholder for). One placeholder and one record, of different addresses.
// The chip names the host alone: one record on the host does not prove it is
// this chip's link. The card lists that record under its own address and never
// presents it as the report.csv link the chip stands for. The query tail the
// split leaves after the placeholder is taken into the chip.
it('one placeholder and one record of different addresses: the record shows its own address, not as the clicked link', () => {
  const { getAllByTestId, getByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  expect(getAllByTestId('blocked-link-inspect')).toHaveLength(1)
  const host = fixture.blocked_links[0].domain
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
  expect(chips[0].textContent).not.toContain(rec.path)
  expect(within(chips[0]).queryByTestId('blocked-link-query')).toBeNull()
})

it('the lone record is listed under its own address, with no claim that it is the clicked link', () => {
  // Nothing proves the one record is the chip's link, so the card frames it
  // the way it frames several: a blocked link to this host, under its own
  // address, below a title that never says "this link".
  const { getByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  fireEvent.click(getByTestId('blocked-link-inspect'))
  const card = getByTestId('blocked-link-card')
  const rec = fixture.blocked_links[0]
  expect(within(card).getByTestId('blocked-link-entry-target').textContent).toBe(`${rec.domain}${rec.path}`)
  expect(card.textContent).toContain(i18nT('components.redaction.link_title_host'))
  expect(card.textContent).not.toMatch(/this link/i)
  // The record kept no address (a credential), so the card offers no way to open it.
  expect(within(card).queryByTestId('blocked-link-review')).toBeNull()
  expect(within(card).queryByTestId('blocked-link-open-once')).toBeNull()
  expect(within(card).getByTestId('blocked-link-withheld')).toBeTruthy()
})

it('the presigned query tail glued to the placeholder is taken into the chip, not left as text beside it', () => {
  const { container, getByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  const text = container.textContent ?? ''
  expect(text).not.toContain('X-Amz-Date')
  expect(text).not.toContain('X-Amz-Signature')
  expect(text).not.toContain('a'.repeat(64))
  // The prose after the tail stays, and nothing became an anchor.
  expect(getByTestId('blocked-link-chip').parentElement?.textContent).toMatch(/Blocked link.*Inspect \. plain/)
  for (const a of Array.from(container.querySelectorAll('a'))) expect(a.textContent).not.toContain('X-Amz-Date')
  // The card never shows the tail either.
  fireEvent.click(getByTestId('blocked-link-inspect'))
  expect(getByTestId('blocked-link-card').textContent).not.toContain('X-Amz')
})
