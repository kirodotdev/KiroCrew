import { expect } from 'vitest'
import { within } from '@testing-library/react'
import { i18nT } from '../i18n/t'

interface Rec { domain: string; path: string | null }

/**
 * The blocked-link card lists the host's records and claims nothing about the
 * click: the title names the set at every count, every entry shows its own
 * address in record order, no entry is marked, and no line says which link
 * was clicked (counts cannot prove a chip and a record are the same URL).
 */
export function expectCardListsHostRecords(card: HTMLElement, records: readonly Rec[], host: string): void {
  const own = records.filter(r => r.domain === host)
  const n = own.length
  // The set title (the English forms are pinned literally in
  // MarkdownRenderer.blockedLink.test.tsx).
  expect(card.textContent).toContain(i18nT('components.redaction.link_title_set', { count: n, host }))
  expect(card.textContent).not.toMatch(/you clicked|This link/i)
  const entries = within(card).getAllByTestId('blocked-link-entry')
  expect(entries.filter(e => e.getAttribute('aria-current') !== null)).toEqual([])
  expect(entries.map(e => within(e).getByTestId('blocked-link-entry-target').textContent))
    .toEqual(own.map(r => (r.path != null ? `${r.domain}${r.path}` : r.domain)))
}
