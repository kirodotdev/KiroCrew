import { it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import fixture from './fixtures/blockedLinkOneOne.json'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'

// Backend output of TestBlockedLinkCardRecords._one_presigned_one_key_split_flush
// (test/test_chat_runner_coverage.py), byte for byte: ONE presigned S3 link
// split at &X-Amz-Date (a placeholder with no record of its own) beside ONE
// same-host URL whose key a delta redacted (a record the text shows no
// placeholder for). One placeholder and one record, of different addresses.
// The card lists that record under its own address, /other-a, and never
// presents it as the report.csv link the chip stands for.
it('one placeholder and one record of different addresses: the record shows its own address, not as the clicked link', () => {
  const { getAllByTestId, getByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  expect(getAllByTestId('blocked-link-inspect')).toHaveLength(1)
  const host = fixture.blocked_links[0].domain
  fireEvent.click(getAllByTestId('blocked-link-inspect')[0])
  expectCardListsHostRecords(getByTestId('blocked-link-card'), fixture.blocked_links, host)
  expect(getByTestId('blocked-link-card').textContent).not.toContain('report.csv')
})
