import { describe, expect, it } from 'vitest'
import { defaultMessageRenderers } from '../app-sdk/messageRenderers'
import { isCrewmateChatRow } from '../components/chat/crewmateBubbles'
import type { ChatMessage } from '../types'

const row = (role: ChatMessage['role'], meta: Record<string, unknown>, content = ''): ChatMessage => ({
  role, content, cls: role === 'assistant' ? 'msg msg-a' : '', meta,
})

describe('warm task brief transcript rows', () => {
  it('hides only the durable claim', () => {
    const claim = row('notice', { kind: 'warm_task_brief_claim', fingerprint: 'abc' })
    const renderer = defaultMessageRenderers.find(item => item.id === 'warm_task_brief_claim')!
    expect(renderer.match?.(claim)).toBe(true)
    expect(renderer.render(claim, {} as never)).toBeNull()
    expect(isCrewmateChatRow(claim)).toBe(false)
  })

  it('draws preparing and result rows as ordinary crewmate messages', () => {
    const preparing = row('assistant', { kind: 'warm_task_brief_preparing' }, 'Welcome back, let me catch you up on where we are.')
    const result = row('assistant', { kind: 'warm_task_brief_result' }, 'You asked me to fix the test. It is ready now. Want to review it?')
    expect(isCrewmateChatRow(preparing)).toBe(true)
    expect(isCrewmateChatRow(result)).toBe(true)
    expect(defaultMessageRenderers.some(renderer => renderer.id === 'warm_task_brief')).toBe(false)
  })
})
