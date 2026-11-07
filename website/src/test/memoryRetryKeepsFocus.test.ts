import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'

// A memory card's "Retry reading" / "Retry identity" button must stay in the tab
// order while its read runs. `disabled` takes a focused button out of the tab
// order, so a keyboard user who pressed it lands on the page body for the whole
// retry. The busy state is `aria-disabled` plus a guard in the click handler,
// like the Apps store Refresh (DiscoverPage.tsx).
const OVERVIEW = join(__dirname, '..', 'pages', 'overview')
const RETRY_KEY = 'memoryV2.retry_'

/** Each `<Btn ...>` opening tag whose children name a memory retry key, with its
 *  file and line. Walks the source with indexOf, no regex. */
function retryButtons(): Array<{ where: string; tag: string }> {
  const found: Array<{ where: string; tag: string }> = []
  for (const name of readdirSync(OVERVIEW)) {
    if (!name.endsWith('.tsx') || name.includes('.test.')) continue
    const src = readFileSync(join(OVERVIEW, name), 'utf8')
    let at = src.indexOf(RETRY_KEY)
    while (at !== -1) {
      const open = src.lastIndexOf('<Btn', at)
      const close = src.indexOf('</Btn>', open)
      if (open !== -1 && close > at) {
        const line = src.slice(0, open).split('\n').length
        found.push({ where: `${name}:${line}`, tag: src.slice(open, at) })
      }
      at = src.indexOf(RETRY_KEY, at + RETRY_KEY.length)
    }
  }
  return found
}

describe('memory retry buttons keep keyboard focus while busy', () => {
  it('scans the real overview cards', () => {
    expect(retryButtons().length).toBeGreaterThanOrEqual(7)
  })

  it('mark busy with aria-disabled, never disabled', () => {
    const bad = retryButtons()
      // The handler guard is what keeps an aria-disabled button from firing.
      .filter(({ tag }) => /\sdisabled=\{/.test(tag) || !tag.includes('aria-disabled=') || !tag.includes('if (!'))
      .map(({ where }) => where)
    expect(bad).toEqual([])
  })
})
