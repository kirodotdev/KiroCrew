/** Both chat surfaces ask "did this staged send land?" and each had spelled the row scan itself, so
 *  the two could answer differently for the same send. This pins the single owner AND that neither
 *  surface re-spells it -- a unit test alone cannot detect a caller drifting back to its own copy.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { sendDelivered } from './sendDelivery'

const SURFACES = ['src/components/ChatPane.tsx', 'src/pages/ChatPage.tsx']
// Resolved from the runner's cwd: the module-url global is undefined under this transform, and a
// URL built on it silently resolves to the literal path "undefined" instead of throwing.
const read = (f: string) => readFileSync(resolve(process.cwd(), f), 'utf-8')

describe('one owner decides whether a staged send was delivered', () => {
  it('reads the confirmation off the send\u2019s own row', () => {
    const rows = [
      { role: 'user', meta: { sendId: 'other', deliveryConfirmed: true } },
      { role: 'user', meta: { sendId: 's-1', deliveryConfirmed: true } },
    ]
    expect(sendDelivered(rows, 's-1')).toBe(true)
    expect(sendDelivered(rows, 's-missing')).toBe(false)
    expect(sendDelivered([], 's-1')).toBe(false)
  })

  it('ignores a confirmation carried by a NON-user row', () => {
    // Positive control: dropping the role test would satisfy the case above and let an assistant
    // row silently confirm the user's send.
    expect(sendDelivered([{ role: 'assistant', meta: { sendId: 's-1', deliveryConfirmed: true } }], 's-1'))
      .toBe(false)
  })

  it('is not re-spelled by either surface', () => {
    for (const f of SURFACES) {
      const src = read(f)
      expect(src.includes('sendDelivered('), `${f} routes through the owner`).toBe(true)
      expect(src.includes('confirmsSend('),
        `${f} must not re-spell the row scan -- that is how the two answers drift apart`).toBe(false)
    }
  })
})
