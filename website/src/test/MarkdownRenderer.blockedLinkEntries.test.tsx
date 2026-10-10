import { describe, it, expect } from 'vitest'
import { render, fireEvent, within, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { expectCardListsHostRecords } from './blockedLinkCardAsserts'

// Seeded random interleavings of blocked links from two hosts across blocks,
// with records that may not match the placeholders one to one (a repeated
// address, a placeholder no record describes, a record whose placeholder the
// text lost). Whichever chip is clicked, exactly one card opens, under the
// clicked chip's block, and its entries are exactly the host's records, in
// record order, each under its own address. No entry is presented as the
// clicked link, at any count.

const HOSTS = ['a.example-sink.net', 'b.example-sink.net']
const PH = (h: string) => `[REDACTED: suspicious URL to ${h}]`

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
  const blocks: string[] = []
  const chips = new Map<string, number>()
  for (let b = 0, n = 1 + pick(4); b < n; b++) {
    const parts = [`Block ${b}:`]
    for (let k = 0, m = pick(3); k < m; k++) {
      const h = HOSTS[pick(2)]
      parts.push(PH(h))
      chips.set(h, (chips.get(h) ?? 0) + 1)
    }
    blocks.push(parts.join(' '))
  }
  const records = HOSTS.flatMap(h => {
    const n = (chips.get(h) ?? 0) === 0 ? 0 : pick(4)
    return Array.from({ length: n }, (_, i) => ({ domain: h, rule: 'exfil_query_length', path: `/p${i}`, query_chars: 290, url: `https://${h}/p${i}?q=x`, url_withheld: null }))
  })
  const { getAllByTestId, queryAllByTestId, container } = render(<MarkdownRenderer content={blocks.join('\n\n')} blockedLinks={records} slotKey="s1" />)
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
  // The invariant: the card's entries are the host's records, one for one.
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
