import { describe, expect, it } from 'vitest'
import type { RootState } from '../index'
import type { ChatMessage } from '../../types'
import { selectSlotTurnFromWake } from './selectors'

const SLOT = 'conductor'

function stateWith(messages: ChatMessage[]): RootState {
  return {
    chat: { activeSlot: 'other', messages: [], slotMessages: { [SLOT]: messages } },
    dashboard: { slots: [{ key: SLOT }] },
  } as unknown as RootState
}

describe('selectSlotTurnFromWake', () => {
  it('reads a turn a monitor wake opened as a wake turn', () => {
    const msgs: ChatMessage[] = [
      { role: 'nudge', content: '[auto-nudge cycle 3]\nwake', cls: 'msg msg-nudge', meta: { nudge: { cycle: 3 } } },
    ]
    expect(selectSlotTurnFromWake(stateWith(msgs), SLOT)).toBe(true)
  })

  it('reads past a wake steered into the person\u2019s running turn (#22747)', () => {
    const msgs: ChatMessage[] = [
      { role: 'user', content: 'build the report', cls: 'msg msg-u' },
      { role: 'assistant', content: 'working on it', cls: 'msg msg-a' },
      { role: 'nudge', content: '[work-ledger wake] item=it_1', cls: 'msg msg-nudge', meta: { steer: true, nudge: { cycle: 4, loop_id: 'l1' } } },
    ]
    expect(selectSlotTurnFromWake(stateWith(msgs), SLOT)).toBe(false)
  })
})
