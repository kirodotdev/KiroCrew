import { describe, it, expect } from 'vitest'
import type { ChatMessage } from '../types'
import { deriveFollowUpOptions } from '../app-sdk/protocol'

const user = (content: string): ChatMessage => ({ role: 'user', content, cls: 'msg msg-u' })
const assistant = (content: string): ChatMessage => ({ role: 'assistant', content, cls: 'msg msg-a' })
// Live websocket path tags the notice with a top-level `kind`.
const compactionLive = (content = '✅ Conversation compacted: summary'): ChatMessage =>
  ({ role: 'assistant', content, cls: 'msg msg-a', kind: 'compaction', meta: { kind: 'compaction' } })
// History-reload path only carries `meta.kind` (append persists meta, not a top-level kind).
const compactionReload = (content = '✅ Conversation compacted: summary'): ChatMessage =>
  ({ role: 'assistant', content, cls: 'msg msg-a', meta: { kind: 'compaction' } })

const OPTIONS_MSG = 'Pick one [OPTIONS: Alpha | Beta | Gamma]'

describe('deriveFollowUpOptions', () => {
  it('returns options from the last assistant turn', () => {
    const { followUpOptions } = deriveFollowUpOptions([user('go'), assistant(OPTIONS_MSG)], false)
    expect(followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('returns no options while streaming', () => {
    expect(deriveFollowUpOptions([user('go'), assistant(OPTIONS_MSG)], true).followUpOptions).toEqual([])
  })

  it('clears options once the user has replied', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG), user('Alpha')]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })

  // Regression: an auto-compaction notice is appended as an assistant-role
  // message AFTER the options-bearing turn. It must not shadow those options.
  it('keeps options when a compaction notice (live kind) follows the options turn', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionLive()]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('keeps options when a compaction notice (reloaded meta.kind) follows the options turn', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionReload()]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('skips multiple stacked compaction notices', () => {
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionLive(), compactionReload()]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual(['Alpha', 'Beta', 'Gamma'])
  })

  it('still stops at a user message that follows a compaction notice', () => {
    // user reply after compaction → previous turn is over, no options
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionLive(), user('next')]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })

  it('returns no options when there is no assistant turn', () => {
    expect(deriveFollowUpOptions([user('hi')], false).followUpOptions).toEqual([])
  })

  // Regression: Quick Send while the slot is busy appends a 'queued' bubble
  // instead of an optimistic 'user' bubble. Options must still vanish.
  it('clears options when a queued message follows the options turn', () => {
    const queued: ChatMessage = { role: 'queued', content: 'Alpha', cls: 'msg msg-queued', meta: { queueId: 'q1' } }
    const msgs = [user('go'), assistant(OPTIONS_MSG), queued]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })

  it('clears options when a queued message follows a compaction notice after options', () => {
    const queued: ChatMessage = { role: 'queued', content: 'Beta', cls: 'msg msg-queued', meta: { queueId: 'q2' } }
    const msgs = [user('go'), assistant(OPTIONS_MSG), compactionLive(), queued]
    expect(deriveFollowUpOptions(msgs, false).followUpOptions).toEqual([])
  })
})
