import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

// A memory read's button (a card's "Retry reading" / "Retry identity", a
// Retry, Show more or Load more) must stay in the tab order while its read
// runs. `disabled` takes a focused button out of the tab order, so a keyboard
// user who pressed it lands on the page body for the whole read. The busy
// state is `aria-disabled` plus a guard in the click handler, like the Apps
// store Refresh (DiscoverPage.tsx).
const OVERVIEW = join(__dirname, '..', 'pages', 'overview')
// The vector card's Retry label also sits on two writes, which are not reads.
const VECTOR_RETRY = "'pages.overview.vectorMemoryCard.retry'"
const VECTOR_WRITES = ['onClick={startEmbeddings}', 'deleteEpisodic.mutate(']
// [catalog key the button renders, how many read buttons render it]
const READ_BUTTONS: Array<[string, number]> = [
  ['memoryV2.retry_', 7],
  ["'memoryV2.show_more'", 3],
  ["'pages.overview.memoryGraphTab.retry'", 1],
  [VECTOR_RETRY, 5],
  ["'pages.overview.vectorMemoryCard.load_more'", 2],
]

/** Each `<Btn ...>` opening tag whose children name `key`, with its file and
 *  line. Walks the source with indexOf, no regex. */
function readButtons(key: string): Array<{ where: string; tag: string }> {
  const found: Array<{ where: string; tag: string }> = []
  for (const name of readdirSync(OVERVIEW)) {
    if (!name.endsWith('.tsx') || name.includes('.test.')) continue
    const src = readFileSync(join(OVERVIEW, name), 'utf8')
    let at = src.indexOf(key)
    while (at !== -1) {
      const open = src.lastIndexOf('<Btn', at)
      const close = src.indexOf('</Btn>', open)
      const tag = src.slice(open, at)
      const write = key === VECTOR_RETRY && VECTOR_WRITES.some(w => tag.includes(w))
      if (open !== -1 && close > at && !write) {
        const line = src.slice(0, open).split('\n').length
        found.push({ where: `${name}:${line}`, tag })
      }
      at = src.indexOf(key, at + key.length)
    }
  }
  return found
}

describe('memory read buttons keep keyboard focus while busy', () => {
  it.each(READ_BUTTONS)('scans the real overview cards for %s', (key, least) => {
    expect(readButtons(key).length).toBeGreaterThanOrEqual(least)
  })

  it('mark busy with aria-disabled, never disabled', () => {
    const bad = READ_BUTTONS.flatMap(([key]) => readButtons(key))
      // The handler guard is what keeps an aria-disabled button from firing.
      .filter(({ tag }) => /\sdisabled=\{/.test(tag) || !tag.includes('aria-disabled=') || !tag.includes('if (!'))
      .map(({ where }) => where)
    expect(bad).toEqual([])
  })
})
