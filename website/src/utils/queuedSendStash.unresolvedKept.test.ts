/** A pre-send record is written BEFORE the POST and only leaves the store when its outcome is
 *  settled, so every record present is still LIVE.
 *
 *  A size cap could therefore only ever drop a live one: the later cancellation falls back to the
 *  server's copy, which has the image @-tokens erased, so the attachments are gone and nothing
 *  downstream can reconstruct them.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import {
  preSendStash, queuedSendStash, stashPreSend, retirePreSendStash, adoptPreSendStash,
} from './queuedSendStash'

const rec = (n: number) => ({ raw: `prompt ${n}`, files: [`file-${n}.png`], sent: `prompt ${n}` })

describe('an UNRESOLVED pre-send record is never evicted', () => {
  beforeEach(() => { preSendStash.clear(); queuedSendStash.clear() })

  it('keeps the oldest live record past any plausible cap', () => {
    for (let n = 0; n < 40; n++) stashPreSend(`s-${n}`, rec(n))

    expect(preSendStash.size, 'every write is still unresolved, so every one is still live').toBe(40)
    expect(preSendStash.get('s-0')?.files,
      'the oldest live record still carries the attachments a cancel cannot rebuild')
      .toEqual(['file-0.png'])
  })

  it('still retires a record once its outcome SETTLES it', () => {
    // Positive control: never removing anything would satisfy the case above while leaking every
    // record, so the two real retirement paths must still empty the store.
    stashPreSend('s-refused', rec(1))
    stashPreSend('s-pushed', rec(2))

    retirePreSendStash('s-refused')
    adoptPreSendStash('s-pushed', 'q-9', 'prompt 2')

    expect(preSendStash.size, 'settlement is what removes a record, not age').toBe(0)
    expect(queuedSendStash.get('q-9')?.files, 'and adoption carries the attachments across')
      .toEqual(['file-2.png'])
  })

  it('overwrites a rewritten record in place rather than duplicating it', () => {
    stashPreSend('s-edit', rec(1))
    stashPreSend('s-edit', rec(2))

    expect(preSendStash.size).toBe(1)
    expect(preSendStash.get('s-edit')?.raw).toBe('prompt 2')
  })
})
