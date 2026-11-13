/**
 * Shrink-only enforcement for the raw-`fetch('/api/…')` gate inventory (#18723).
 *
 * `eslint.config.js` exempts a standing map of files from the gate so they can
 * migrate to the typed `api.*` facade one at a time. The danger of any such
 * allowlist is twofold: it rots into a blanket exemption (a file migrates, its
 * raw fetches are gone, but the entry lingers and silently permits a NEW raw
 * fetch there forever), and a busy already-listed file quietly GAINS a raw fetch
 * under its own exemption.
 *
 * This test closes both edges by pinning the EXACT count of raw API fetches per
 * listed file. A count that DROPS means the file migrated (fully, when it hits
 * 0) and the entry must be lowered or removed; a count that RISES means a new
 * raw fetch slipped in and must instead become a typed `api.*` method. Either
 * way this reddens until the map matches reality. The count only ever decreases.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { RAW_API_FETCH_INVENTORY } from '../../eslint.config.js'

const WEBSITE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

// The same two shapes the eslint gate matches: a string or template-literal
// first argument to `fetch(` whose path begins `/api/`.
const RAW_API_FETCH = /fetch\(\s*[`'"]\/api\//g

function countRawApiFetches(relPath: string): number {
  const src = readFileSync(resolve(WEBSITE_ROOT, relPath), 'utf8')
  return (src.match(RAW_API_FETCH) ?? []).length
}

const entries = Object.entries(RAW_API_FETCH_INVENTORY) as [string, number][]

describe('raw /api fetch inventory (#18723) only shrinks', () => {
  it('lists real files', () => {
    expect(entries.length).toBeGreaterThan(0)
  })

  it.each(entries)('%s holds exactly its pinned count of raw fetch("/api/…")', (relPath, expected) => {
    const actual = countRawApiFetches(relPath)
    expect(actual, actual < expected
      ? `${relPath} now has ${actual} raw fetch('/api/…') calls, fewer than the pinned ${expected} — it migrated some to the journaled transport. Lower its count in RAW_API_FETCH_INVENTORY (eslint.config.js), or remove it entirely when the count reaches 0.`
      : `${relPath} now has ${actual} raw fetch('/api/…') calls, MORE than the pinned ${expected} — a new raw fetch was added under its gate exemption. Route the new request through a typed api.* method instead of raising this count.`,
    ).toBe(expected)
  })

  it('every pinned count is positive (a 0 entry must be deleted, not kept)', () => {
    for (const [relPath, count] of entries) {
      expect(count, `${relPath} is pinned at 0 — remove it from the inventory`).toBeGreaterThan(0)
    }
  })

  it('stays sorted by path (a sorted map is easy to audit and to delete from)', () => {
    const keys = entries.map(([k]) => k)
    expect(keys).toEqual([...keys].sort())
  })
})
