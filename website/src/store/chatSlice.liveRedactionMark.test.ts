import { describe, it, expect } from 'vitest'
import reducer, { sseChatMessage } from './chatSlice'
import '../test/mockApiClient'

/**
 * A live `chat_message` frame must carry the row's rewrite mark into the store.
 *
 * Two backend frame builders emit it, but the store rebuilt each row from a fixed
 * field list and dropped it, so the cue appeared only after a history reload. There
 * are TWO hand-synced appliers and the bug was in both, so each is pinned separately:
 * `sseChatMessage` for the active slot, `applyNonActiveFrame` (any other slot) for the
 * background caches. Fixing one alone leaves the other silently wrong.
 */

const SLOT = 'chat-redaction'
const OTHER = 'chat-elsewhere'
const init = () => ({ ...reducer(undefined, { type: '@@INIT' }), activeSlot: SLOT })

const REWRITTEN = 'deploy with token [REDACTED: credential] and retry'
const VERBATIM = 'deploy with the staging profile and retry'

describe('live redaction mark on a chat_message frame', () => {
  it('the active-slot applier keeps the mark', () => {
    const state = reducer(
      init(),
      sseChatMessage({ slot: SLOT, role: 'user', content: REWRITTEN, redacted: true }),
    )
    expect(state.messages.at(-1)?.redacted).toBe(true)
  })

  it('the background applier keeps the mark', () => {
    const state = reducer(
      init(),
      sseChatMessage({ slot: OTHER, role: 'user', content: REWRITTEN, redacted: true }),
    )
    expect(state.slotMessages[OTHER]?.at(-1)?.redacted).toBe(true)
  })

  // The cue claims the author's own words were mutated, so a frame without the
  // mark must not acquire one -- over-marking is the failure it exists to avoid.
  it('an unmarked frame stays unmarked in either applier', () => {
    const active = reducer(init(), sseChatMessage({ slot: SLOT, role: 'user', content: VERBATIM }))
    const background = reducer(init(), sseChatMessage({ slot: OTHER, role: 'user', content: VERBATIM }))
    expect(active.messages.at(-1)?.redacted).toBeUndefined()
    expect(background.slotMessages[OTHER]?.at(-1)?.redacted).toBeUndefined()
  })

  it('redacted: false is not promoted to a mark', () => {
    const state = reducer(
      init(),
      sseChatMessage({ slot: SLOT, role: 'user', content: VERBATIM, redacted: false }),
    )
    expect(state.messages.at(-1)?.redacted).toBeUndefined()
  })
})
