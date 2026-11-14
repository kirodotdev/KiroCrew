import { describe, expect, it } from 'vitest'
import type { ChatMessage } from '../../types'
import { replyOfLastTurn } from './voicePlayback'

describe('replyOfLastTurn', () => {
  it('selects the visible reply before a hidden steering-only row', () => {
    const visible = { role: 'assistant', content: 'The visible result is ready.' } as ChatMessage
    const messages = [
      { role: 'user', content: 'Check the result' },
      visible,
      { role: 'inject', content: '[Tool blocked — reason sent to the agent] policy' },
      { role: 'assistant', content: '[STEERING steer-ab12: Recorded the blocked command.]' },
    ] as ChatMessage[]

    expect(replyOfLastTurn(messages)).toBe(visible)
  })
})
