import { describe, it, expect } from 'vitest'
import { render, fireEvent, within, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'

// Seeded random interleavings of links to two hosts across blocks, some of
// them recorded as blocked (a link may repeat a recorded address) and some
// not, which render live. Whichever chip is clicked, exactly one card opens,
// under the clicked chip's block, and its entries are exactly the host's
// records in record order. No entry is presented as the clicked link, at any
// count.

const HOSTS = ['a.example-sink.net', 'b.example-sink.net']
const recordUrl = (h: string, i: number) => `https://${h}/p${i}?q=x`

function rng(seed: number): () => number {
  let s = seed >>> 0
  return () => {
    s = (s + 0x6d2b79f5) >>> 0
    let t = s
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function check(seed: number): void {
  const r = rng(seed)
  const pick = (n: number) => Math.floor(r() * n)
  const counts = HOSTS.map(() => pick(4))
  const records = HOSTS.flatMap((h, hi) => Array.from({ length: counts[hi] }, (_, i) => (
    { url: recordUrl(h, i), domain: h, rule: 'exfil_query_length', query_chars: 290, withheld: null })))
  const blocks: string[] = []
  const live: string[] = []
  for (let b = 0, n = 1 + pick(4); b < n; b++) {
    const parts = [`Block ${b}:`]
    for (let k = 0, m = pick(3); k < m; k++) {
      const hi = pick(2)
      const h = HOSTS[hi]
      // A recorded address, or one no record names, which renders live.
      if (counts[hi] > 0 && pick(3) > 0) parts.push(recordUrl(h, pick(counts[hi])))
      else {
        const free = `https://${h}/free${b}${k}`
        live.push(free)
        parts.push(free)
      }
    }
    blocks.push(parts.join(' '))
  }
  const { getAllByTestId, queryAllByTestId, container } = render(<MarkdownRenderer content={blocks.join('\n\n')} blockedLinks={records} slotKey="s1" />)
  for (const free of live) expect(container.querySelector(`a[href="${free}"]`)).toBeTruthy()
  for (const rec of records) expect(container.querySelector(`a[href="${rec.url}"]`)).toBeNull()
  const inspect = queryAllByTestId('blocked-link-inspect')
  if (inspect.length === 0) { cleanup(); return }
  const clicked = inspect[pick(inspect.length)]
  fireEvent.click(clicked)
  const cards = getAllByTestId('blocked-link-card')
  expect(cards).toHaveLength(1)
  const card = cards[0]
  expect(clicked.getAttribute('aria-controls')).toBe(card.id)
  // The card opens right after the block holding the clicked chip.
  const block = clicked.closest('p')
  expect(block).not.toBeNull()
  expect(block!.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const between = [...container.querySelectorAll('p')].filter(p => block!.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING && p.compareDocumentPosition(card) & Node.DOCUMENT_POSITION_FOLLOWING)
  expect(between.filter(p => !card.contains(p))).toEqual([])
  const host = card.id.slice('rx-link-'.length).split('~')[0]
  // The invariant: the card's entries are the host's records, one for one,
  // in record order.
  expectCardListsHostRecords(card, records, host)
  expect(within(card).getAllByTestId('blocked-link-entry')).toHaveLength(records.filter(x => x.domain === host).length)
  cleanup()
}

// 300 seeds in three blocks so each stays well inside the per-test budget.
describe('blocked-link card entries over random interleavings', () => {
  for (const start of [0, 100, 200]) {
    it(`seeds ${start}..${start + 99}`, () => {
      for (let seed = start; seed < start + 100; seed++) check(seed)
    })
  }
})
