import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

/**
 * Every read of a slot's `last_ts` goes through `store/slotLastTs`.
 *
 * Mid-turn bumps live in `dashboard.slotLastTs`, not on the row, so a direct
 * `row.last_ts` read in a new place sees a value that can be a whole turn old:
 * a stale unread watermark, or a recency order that stops moving. A read that
 * genuinely may use the row value (it gets rows already merged with the
 * overlay, or it is reached only for rows that take every bump) says so on the
 * line with a `// last-ts: <why>` marker.
 *
 * A text rule: it sees `.last_ts` and `['last_ts']`, not a destructured
 * `{ last_ts }`. Comment lines are skipped, and tests may read rows freely.
 */

const SRC = join(process.cwd(), 'src')
const HELPER = join('store', 'slotLastTs.ts')
const READ = /\.last_ts\b|\[['"]last_ts['"]\]/
const MARKER = /\/\/\s*last-ts:\s*\S/
const COMMENT = /^\s*(\*|\/\/|\/\*)/

const sources = (dir: string): string[] => readdirSync(dir).flatMap(name => {
  const path = join(dir, name)
  if (statSync(path).isDirectory()) return name === 'test' || name === '__tests__' ? [] : sources(path)
  return /\.tsx?$/.test(name) && !/\.(test|spec)\.tsx?$/.test(name) ? [path] : []
})

describe('last_ts readers', () => {
  it('detects the shapes it polices', () => {
    expect(READ.test('const t = slot.last_ts')).toBe(true)
    expect(READ.test("const t = slot['last_ts']")).toBe(true)
    expect(READ.test('last_ts?: string')).toBe(false)
    expect(READ.test('slot.last_turn_ts')).toBe(false)
  })

  it('has no unmarked read outside store/slotLastTs', () => {
    const offenders = sources(SRC)
      .filter(path => relative(SRC, path) !== HELPER)
      .flatMap(path => readFileSync(path, 'utf8').split('\n')
        .map((text, i) => ({ text, line: i + 1 }))
        .filter(({ text }) => READ.test(text) && !COMMENT.test(text) && !MARKER.test(text))
        .map(({ text, line }) => `${relative(SRC, path)}:${line}: ${text.trim()}`))
    expect(offenders).toEqual([])
  })
})
