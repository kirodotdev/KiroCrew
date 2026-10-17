import { describe, expect, it } from 'vitest'

import {
  deliveryVerdict,
  needsDeliveryRetry,
  transcriptHasRequest,
} from '../apps/design-tweak/delivery'

const RID = '1754350412345-a1b2c3'

/** A prompt shaped like the real one: the id appears in the payload path. */
const promptFor = (id: string) =>
  'Apply 2 visual edits from Design Tweak request 3.\n' +
  `The full payload is at /Users/x/.kiro/crew/apps/design-tweak/data/queue/${id}.json`

describe('design-tweak delivery verification', () => {
  describe('needsDeliveryRetry', () => {
    it('flags a request with no delivery confirmation', () => {
      expect(needsDeliveryRetry({ deliveredAt: '' })).toBe(true)
      expect(needsDeliveryRetry({})).toBe(true)
    })

    it('leaves a confirmed request alone', () => {
      expect(needsDeliveryRetry({ deliveredAt: '2026-08-04T20:00:00Z' })).toBe(false)
    })
  })

  describe('transcriptHasRequest', () => {
    it('finds the batch in the transcript', () => {
      const t = { messages: [{ role: 'user', content: promptFor(RID) }] }
      expect(transcriptHasRequest(t, RID)).toBe(true)
    })

    it('finds the batch in the PENDING QUEUE', () => {
      // Load-bearing: seconds after a send the prompt is accepted but not yet a
      // message. A transcript-only check would read "absent" and invite a resend
      // that duplicates every edit.
      const t = { messages: [], queue: [{ id: 'q1', content: promptFor(RID) }] }
      expect(transcriptHasRequest(t, RID)).toBe(true)
    })

    it('does not match a DIFFERENT request in the same session', () => {
      const t = { messages: [{ role: 'user', content: promptFor('1754350499999-zzzzzz') }] }
      expect(transcriptHasRequest(t, RID)).toBe(false)
    })

    it('is false for an empty session', () => {
      expect(transcriptHasRequest({ messages: [], queue: [] }, RID)).toBe(false)
    })

    it('never matches on an empty id', () => {
      // Guards the degenerate case: `''` is a substring of every string, so a
      // missing id must not make every request look delivered.
      const t = { messages: [{ role: 'user', content: promptFor(RID) }] }
      expect(transcriptHasRequest(t, '')).toBe(false)
    })

    it('tolerates malformed entries rather than throwing', () => {
      const t = {
        messages: [null, 'not-an-object', { role: 'user' }, { content: 42 }] as unknown[],
        queue: [undefined] as unknown[],
      }
      expect(transcriptHasRequest(t, RID)).toBe(false)
    })

    it('is false with no transcript at all', () => {
      expect(transcriptHasRequest(null, RID)).toBe(false)
    })
  })

  describe('deliveryVerdict', () => {
    const req = { id: RID, deliveredAt: '' }

    it('short-circuits on an already-confirmed request', () => {
      const confirmed = { id: RID, deliveredAt: '2026-08-04T20:00:00Z' }
      expect(deliveryVerdict(confirmed, null)).toBe('delivered')
    })

    it('reports delivered when the session has the batch', () => {
      const t = { messages: [{ role: 'user', content: promptFor(RID) }] }
      expect(deliveryVerdict(req, t)).toBe('delivered')
    })

    it('reports missing when the session does not have it', () => {
      expect(deliveryVerdict(req, { messages: [], queue: [] })).toBe('missing')
    })

    it('reports unknown — NOT missing — when the lookup failed', () => {
      // THE invariant. A failed lookup must not read as "missing", or the panel
      // would offer a send for a batch the agent may already be working on.
      expect(deliveryVerdict(req, null)).toBe('unknown')
    })

    it('reports unknown — NOT missing — when the window is truncated', () => {
      // Same invariant, second way in. Both slot reads cap at 200 rows, so a
      // request older than the window is absent from a transcript it WAS
      // delivered into. Calling that "missing" resends the batch and reapplies
      // every completed edit.
      const truncated = { messages: [], queue: [], hasMore: true }
      expect(deliveryVerdict(req, truncated)).toBe('unknown')
    })

    it('still reports missing when the whole history was searched', () => {
      // The flag must not blanket-suppress `missing`: a genuinely undelivered
      // batch in a COMPLETE transcript still has to be resendable, otherwise the
      // retry path this verdict drives would never fire at all.
      expect(deliveryVerdict(req, { messages: [], queue: [], hasMore: false })).toBe('missing')
    })

    it('reports delivered from a truncated window that DOES contain the batch', () => {
      // Truncation only clouds absence. A hit is still a hit.
      const t = { messages: [{ role: 'user', content: promptFor(RID) }], hasMore: true }
      expect(deliveryVerdict(req, t)).toBe('delivered')
    })
  })
})
