import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ChatMessage, TurnIdentityFields } from '../../types'
import { useCrewmateAwaitingReply } from './useCrewmateAwaitingReply'

type Props = { slot: string; all: ChatMessage[]; live: boolean; turn?: TurnIdentityFields }

const sent: ChatMessage = { role: 'user', content: 'are you there', cls: 'msg msg-u', meta: { sendId: 'send-1', mid: 'u-1' } }

function mount(initial: Props) {
  return renderHook(({ slot, all, live, turn }: Props) => useCrewmateAwaitingReply(true, slot, all, live, turn), { initialProps: initial })
}

describe('useCrewmateAwaitingReply: the turn identity ends a wait the pane never saw live', () => {
  it('a frame still naming the turn current at Send is not proof: the wait holds', () => {
    const hook = mount({ slot: 'member-a', all: [sent], live: false, turn: { turn: 4, turn_gen: 'gen-a' } })
    act(() => hook.result.current.markSent('send-1'))
    hook.rerender({ slot: 'member-a', all: [sent], live: false, turn: { turn: 4, turn_gen: 'gen-a' } })
    expect(hook.result.current.awaitingReply).toBe(true)
  })

  it('a pane re-pointed at another mate does not end the wait with THAT slot\'s turn', () => {
    const hook = mount({ slot: 'member-a', all: [sent], live: false, turn: { turn: 4, turn_gen: 'gen-a' } })
    act(() => hook.result.current.markSent('send-1'))
    hook.rerender({ slot: 'member-b', all: [], live: false, turn: { turn: 9, turn_gen: 'gen-a' } })
    expect(hook.result.current.awaitingReply).toBe(false)
    hook.rerender({ slot: 'member-a', all: [sent], live: false, turn: { turn: 4, turn_gen: 'gen-a' } })
    expect(hook.result.current.awaitingReply).toBe(true)
  })

  it('a queued send that drains after a frame named another turn waits for its OWN turn', () => {
    const queued: ChatMessage = { role: 'queued', content: 'are you there', cls: '', meta: { sendId: 'send-1' } }
    const hook = mount({ slot: 'member-a', all: [queued], live: false, turn: { turn: 4, turn_gen: 'gen-a' } })
    act(() => hook.result.current.markSent('send-1'))
    // The turn before it ended unseen while this send was parked: the wait goes on.
    hook.rerender({ slot: 'member-a', all: [queued], live: false, turn: { turn: 5, turn_gen: 'gen-a' } })
    expect(hook.result.current.awaitingReply).toBe(true)
    // The drain writes its row; the turn that answers it has not been seen yet.
    hook.rerender({ slot: 'member-a', all: [sent], live: false, turn: { turn: 5, turn_gen: 'gen-a' } })
    expect(hook.result.current.awaitingReply).toBe(true)
    // That turn came and went too, with nothing said.
    hook.rerender({ slot: 'member-a', all: [sent], live: false, turn: { turn: 6, turn_gen: 'gen-a' } })
    expect(hook.result.current.awaitingReply).toBe(false)
  })

  it('a slot with no turn identity keeps the live-edge rule alone', () => {
    const hook = mount({ slot: 'member-a', all: [sent], live: false })
    act(() => hook.result.current.markSent('send-1'))
    hook.rerender({ slot: 'member-a', all: [sent], live: false, turn: { turn: 5, turn_gen: 'gen-a' } })
    expect(hook.result.current.awaitingReply).toBe(true)
  })
})
