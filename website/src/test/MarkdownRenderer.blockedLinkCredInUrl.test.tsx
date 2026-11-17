import { it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import fixture from './fixtures/credInUrl.json'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'

// Four links on one S3 host: two the decision passed render live, and two
// carrying a credential it blocked draw chips. Whichever chip is clicked, the
// card lists every record under its own address and presents none as the
// clicked link.
it('two credential links beside two live links on one host: the card lists every record and marks none', () => {
  const { getAllByTestId, getByTestId, container } = render(
    <MarkdownRenderer content={fixture.content} blockedLinks={fixture.blocked_links} slotKey="s1" />)
  expect(getAllByTestId('blocked-link-inspect')).toHaveLength(2)
  expect(container.querySelectorAll('a[href*="X-Amz-Algorithm"]')).toHaveLength(2)
  fireEvent.click(getAllByTestId('blocked-link-inspect')[1])
  expectCardListsHostRecords(getByTestId('blocked-link-card'), fixture.blocked_links, fixture.blocked_links[0].domain)
})
