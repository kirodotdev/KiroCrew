/**
 * The ChatEmbed hand-back buffer is scoped by APP and slot, not slot alone.
 *
 * Two apps can embed the same slot (permissions are per app; the slot is
 * shared). A receipt parked by one app carries that app's user's text; another
 * app mounting an embed for the same slot must not drain it -- it would render
 * the first app's composer text and delete the only copy. Pinned here at the
 * module seam so the property does not depend on how ChatEmbed wires it.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  drainEmbedRecovery,
  resetEmbedRecoveryForTests,
  stashEmbedRecovery,
  subscribeEmbedRecovery,
  type PendingEmbedRecovery,
} from '../app-sdk/embedSendRecovery'

const rec = (sendId: string, restoreText?: string): PendingEmbedRecovery =>
  ({ tail: { role: 'error', content: 'refused', sendId }, restoreText })

describe('embedSendRecovery scope', () => {
  beforeEach(() => resetEmbedRecoveryForTests())

  it('the same app draining the same slot gets its receipts back, oldest first', () => {
    stashEmbedRecovery({ app: 'a', slot: 's1' }, rec('1', 'first'))
    stashEmbedRecovery({ app: 'a', slot: 's1' }, rec('2', 'second'))
    expect(drainEmbedRecovery({ app: 'a', slot: 's1' }).map(r => r.restoreText)).toEqual(['first', 'second'])
    expect(drainEmbedRecovery({ app: 'a', slot: 's1' })).toEqual([])
  })

  it('another app embedding the same slot cannot drain, or be notified of, the first app\'s receipts', () => {
    const onB = vi.fn()
    subscribeEmbedRecovery({ app: 'b', slot: 's1' }, onB)
    stashEmbedRecovery({ app: 'a', slot: 's1' }, rec('1', 'private to a'))
    expect(onB).not.toHaveBeenCalled()
    expect(drainEmbedRecovery({ app: 'b', slot: 's1' })).toEqual([])
    // Still parked for the app that owns it.
    expect(drainEmbedRecovery({ app: 'a', slot: 's1' }).map(r => r.restoreText)).toEqual(['private to a'])
  })

  it('the same app is notified only for the slot it subscribed to', () => {
    const onS1 = vi.fn()
    const off = subscribeEmbedRecovery({ app: 'a', slot: 's1' }, onS1)
    stashEmbedRecovery({ app: 'a', slot: 's2' }, rec('1'))
    expect(onS1).not.toHaveBeenCalled()
    stashEmbedRecovery({ app: 'a', slot: 's1' }, rec('2'))
    expect(onS1).toHaveBeenCalledTimes(1)
    off()
    stashEmbedRecovery({ app: 'a', slot: 's1' }, rec('3'))
    expect(onS1).toHaveBeenCalledTimes(1)
  })
})
