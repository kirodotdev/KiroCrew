import { it } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import fixture from './fixtures/credInUrl.json'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'

// Backend output of TestBlockedLinkCardRecords._key_split_presigned_flush
// (test/test_chat_runner_coverage.py), byte for byte: S3 presigned URLs split
// at &X-Amz-Date (placeholders with no record of their own) plus same-host
// URLs whose credential was redacted per delta (records with no placeholder in
// the text). The counts can match; the card lists every record under its own
// address and presents none as the clicked link.
it('a key-split link beside presigned starts lists every address and marks none', () => {
  const { getAllByTestId, getByTestId } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  fireEvent.click(getAllByTestId('blocked-link-inspect')[0])
  expectCardListsHostRecords(getByTestId('blocked-link-card'), fixture.blocked_links, fixture.blocked_links[0].domain)
})
