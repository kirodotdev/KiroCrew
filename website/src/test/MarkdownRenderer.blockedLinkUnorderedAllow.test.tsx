import { it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { api } from '../api/client'

// A host with several records: the card lists them and never says which entry
// the chip is. Allow acts on the host once (a card-level control), so no entry
// is ever marked and no entry carries its own Allow or outcome.
const host = 'reviews.example-sink.net'
const PH = `[REDACTED: suspicious URL to ${host}]`
const records = [1, 2, 3].map(i => ({ domain: host, rule: 'exfil_query_length', path: `/p${i}`, query_chars: 290, url: `https://${host}/p${i}?q=x`, url_withheld: null }))
const content = records.map((_, i) => `Link ${i + 1}: ${PH}`).join('\n\n')
afterEach(() => vi.restoreAllMocks())

it('a host with several records never marks an entry, even after Allow (#18675)', async () => {
  vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
  const { getAllByTestId, getByTestId } = render(
    <MarkdownRenderer content={content} blockedLinks={records} slotKey="s1" messageTs="2026-10-08T18:00:00Z" />)
  fireEvent.click(getAllByTestId('blocked-link-inspect')[0])
  const card = () => getByTestId('blocked-link-card')
  const entries = () => within(card()).getAllByTestId('blocked-link-entry')
  expect(entries().map(e => e.getAttribute('aria-current'))).toEqual([null, null, null])
  // One Allow for the host, not one per entry.
  expect(within(card()).getAllByTestId('blocked-link-allow')).toHaveLength(1)
  fireEvent.click(within(card()).getByTestId('blocked-link-allow'))
  fireEvent.click(within(card()).getByTestId('blocked-link-allow-confirmed'))
  await waitFor(() => expect(within(card()).getByTestId('blocked-link-feedback')).toBeTruthy())
  // Still no entry marked, and exactly one host-level outcome — not one per entry.
  expect(entries().map(e => e.getAttribute('aria-current'))).toEqual([null, null, null])
  expect(within(card()).getAllByTestId('blocked-link-feedback')).toHaveLength(1)
  expect(entries().map(e => within(e).queryByTestId('blocked-link-feedback') !== null)).toEqual([false, false, false])
})
