import { expect } from 'vitest'
import { within } from '@testing-library/react'
import { i18nT } from '../i18n/t'

interface Rec { domain: string; path: string | null }

/**
 * The blocked-link card lists the host's records and claims nothing about the
 * click. Its title speaks of the host's blocked links, never "this link", and
 * each entry keeps its Destination line. Every entry, a lone one included,
 * shows its own address, in record order whichever chip was clicked; no entry
 * is marked and no line says which link was clicked (counts cannot prove a
 * chip and a record are the same URL).
 */
export function expectCardListsHostRecords(card: HTMLElement, records: readonly Rec[], host: string): void {
  const own = records.filter(r => r.domain === host)
  expect(card.textContent).toContain(i18nT('components.redaction.link_title_host'))
  expect(card.textContent).not.toMatch(/this link/i)
  expect(card.textContent).not.toMatch(/you clicked/i)
  const entries = within(card).getAllByTestId('blocked-link-entry')
  expect(entries).toHaveLength(own.length)
  expect(entries.filter(e => e.getAttribute('aria-current') !== null)).toEqual([])
  const targets = entries.map(e => within(e).queryByTestId('blocked-link-entry-target')?.textContent ?? null)
  expect(targets).toEqual(own.map(r => (r.path != null ? `${r.domain}${r.path}` : r.domain)))
}
