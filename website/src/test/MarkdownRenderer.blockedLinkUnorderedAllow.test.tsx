import { it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { api } from '../api/client'

// A host with several records: the card lists them and never says which entry
// the chip is. Allow from an entry shows its outcome under that entry and never
// marks any entry as the inspected link.
const host = 'reviews.example-sink.net'
const PH = `[REDACTED: suspicious URL to ${host}]`
const records = [1, 2, 3].map(i => ({ domain: host, rule: 'exfil_query_length', path: `/p${i}`, query_chars: 290, url: `https://${host}/p${i}?q=x`, url_withheld: null }))
const content = records.map((_, i) => `Link ${i + 1}: ${PH}`).join('\n\n')
afterEach(() => vi.restoreAllMocks())

it('a host with several records never marks an entry, even after Allow from one', async () => {
  vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
  const { getAllByTestId, getByTestId } = render(
    <MarkdownRenderer content={content} blockedLinks={records} slotKey="s1" messageTs="2026-10-08T18:00:00Z" />)
  fireEvent.click(getAllByTestId('blocked-link-inspect')[0])
  const entries = () => within(getByTestId('blocked-link-card')).getAllByTestId('blocked-link-entry')
  expect(entries().map(e => e.getAttribute('aria-current'))).toEqual([null, null, null])
  fireEvent.click(within(entries()[2]).getByTestId('blocked-link-allow'))
  fireEvent.click(within(entries()[2]).getByTestId('blocked-link-allow-confirmed'))
  await waitFor(() => expect(within(entries()[2]).getByTestId('blocked-link-feedback')).toBeTruthy())
  expect(entries().map(e => e.getAttribute('aria-current'))).toEqual([null, null, null])
  expect(entries().map(e => within(e).queryByTestId('blocked-link-feedback') !== null)).toEqual([false, false, true])
})
