// The composer's Escape on pending steers (useSteerEscape): which row each press
// takes back, and how presses queue behind the one take-back a chat runs at a time.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ChatMessage } from '../types'
import type { SteerRetractRequestOutcome } from '../hooks/useSteerRetract'
import { useSteerEscape, type SteerEscapeOptions } from '../hooks/useSteerEscape'

// An Edit needs the Web Locks API, which the test DOM lacks: a present `locks`
// is enough here, since the take-back hook itself is a mock.
let restoreLocks: () => void
beforeEach(() => {
  const original = Object.getOwnPropertyDescriptor(navigator, 'locks')
  Object.defineProperty(navigator, 'locks', { configurable: true, value: { request: vi.fn() } })
  restoreLocks = () => {
    if (original) Object.defineProperty(navigator, 'locks', original)
    else delete (navigator as { locks?: unknown }).locks
  }
})
afterEach(() => { restoreLocks() })

const steerRow = (mid: string, meta: Record<string, unknown> = {}, content = `steer ${mid}`): ChatMessage => ({
  role: 'user',
  content,
  cls: 'msg msg-u',
  ts: '2026-11-24T05:00:00Z',
  meta: { steer: true, mid, steerState: 'written', steerRetract: true, ...meta },
})

const bubble = (sendId: string): ChatMessage => ({
  role: 'user',
  content: `bubble ${sendId}`,
  cls: 'msg msg-u',
  ts: '2026-11-24T05:00:01Z',
  meta: { steer: true, optimistic: true, sendId },
})

const EDIT = { prepend: true, retryBusy: true }

function setup(initial: Partial<SteerEscapeOptions> = {}) {
  const answers: Array<(outcome: SteerRetractRequestOutcome) => void> = []
  const onSteerAction = vi.fn(() => new Promise<SteerRetractRequestOutcome>(resolve => { answers.push(resolve) }))
  const props: SteerEscapeOptions = {
    slot: 'chat-a',
    messages: [],
    slotRunning: true,
    onSteerAction,
    pending: {},
    ...initial,
  }
  const hook = renderHook((p: SteerEscapeOptions) => useSteerEscape(p), { initialProps: props })
  const update = (over: Partial<SteerEscapeOptions>) => {
    Object.assign(props, over)
    hook.rerender({ ...props })
  }
  const answer = async (index: number, outcome: SteerRetractRequestOutcome) => {
    await act(async () => { answers[index](outcome) })
  }
  const press = () => {
    let took = false
    act(() => { took = hook.result.current.takeBack() })
    return took
  }
  return { hook, onSteerAction, update, answer, press }
}

describe('useSteerEscape', () => {
  it('counts the steers Escape can take back, and only those', () => {
    const { hook } = setup({
      messages: [
        steerRow('m-1'),
        steerRow('m-2', { steerRetract: false }),
        steerRow('m-3', { steerState: 'consumed' }),
        { role: 'assistant', content: 'reply', cls: 'msg', meta: { mid: 'm-4' } },
        bubble('s-1'),
      ],
    })
    expect(hook.result.current.offered).toBe(1)
    expect(hook.result.current.reachable).toBe(2)
  })

  it('takes back the newest steer first, with Edit put in front of the draft', () => {
    const { hook, onSteerAction, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    expect(press()).toBe(true)
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(onSteerAction).toHaveBeenCalledWith('m-2', 'edit', EDIT)
    expect(hook.result.current.offered).toBe(1)
  })

  it('queues the next press until the take-back before it settles, then sends it', async () => {
    const { hook, onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    update({ pending: { 'm-2': 'edit' } })
    expect(press()).toBe(true)
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(hook.result.current.offered).toBe(0)

    await answer(0, 'accepted')
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    update({ messages: [steerRow('m-1'), steerRow('m-2', { steerState: 'retracted' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-1', 'edit', EDIT)
  })

  it('a press made before the first take-back shows as pending reaches the next older steer', async () => {
    const { onSteerAction, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    expect(press()).toBe(true)
    expect(press()).toBe(true)
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(onSteerAction).toHaveBeenCalledWith('m-2', 'edit', EDIT)
    await answer(0, 'accepted')
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-1', 'edit', EDIT)
  })

  it('answers false when every pending steer is already asked for', () => {
    const { update, press } = setup({ messages: [steerRow('m-1')] })
    press()
    update({ pending: { 'm-1': 'edit' } })
    expect(press()).toBe(false)
  })

  it('leaves a steer whose row control already started a take-back to that take-back, and waits behind it', () => {
    const { hook, onSteerAction, update, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')], pending: { 'm-2': 'queue' } })
    expect(hook.result.current.offered).toBe(1)
    expect(press()).toBe(true)
    expect(onSteerAction).not.toHaveBeenCalled()
    update({ messages: [steerRow('m-1'), steerRow('m-2', { steerState: 'queued' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledWith('m-1', 'edit', EDIT)
  })

  it.each(['refused', 'not_sent'] as const)('drops the queue when a take-back is %s, and the next press asks for the same row again', async (outcome) => {
    const { hook, onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    press()
    await answer(0, outcome)
    update({ pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(hook.result.current.offered).toBe(2)
    expect(press()).toBe(true)
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-2', 'edit', EDIT)
  })

  it('holds the queue behind a take-back whose answer was lost, until its row settles', async () => {
    const { onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    update({ pending: { 'm-2': 'edit' } })
    press()
    await answer(0, 'unknown')
    update({ messages: [steerRow('m-1'), steerRow('m-2')] })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(press()).toBe(false)

    update({ messages: [steerRow('m-1'), steerRow('m-2', { steerState: 'retracted' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-1', 'edit', EDIT)
  })

  it('sends the queued press when a lost answer arrives after its row already settled', async () => {
    const { onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    update({ pending: { 'm-2': 'edit' } })
    press()
    update({ messages: [steerRow('m-1'), steerRow('m-2', { steerState: 'retracted' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    await answer(0, 'unknown')
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-1', 'edit', EDIT)
  })

  it('keeps a row answered as no longer pending out of reach while the transcript reloads without it', async () => {
    const { hook, update, answer, press } = setup({ messages: [steerRow('m-1')] })
    press()
    update({ pending: { 'm-1': 'edit' } })
    await answer(0, 'not_pending')
    update({ pending: {} })
    update({ messages: [], transcriptAuthoritative: false })
    update({ messages: [steerRow('m-1')], transcriptAuthoritative: true })
    expect(hook.result.current.offered).toBe(0)
    expect(press()).toBe(false)
  })

  it('does not pick a row again after the route said its steer was no longer pending, until the row moves on', async () => {
    const { hook, onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1')] })
    press()
    update({ pending: { 'm-1': 'edit' } })
    await answer(0, 'not_pending')
    update({ pending: {} })
    expect(hook.result.current.offered).toBe(0)
    expect(press()).toBe(false)
    expect(onSteerAction).toHaveBeenCalledTimes(1)

    update({ messages: [steerRow('m-1', { steerState: 'consumed' }), steerRow('m-3')] })
    expect(hook.result.current.offered).toBe(1)
    expect(press()).toBe(true)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-3', 'edit', EDIT)
  })

  it('reaches no steer on a page without Web Locks, where an Edit cannot be sent', () => {
    const original = Object.getOwnPropertyDescriptor(navigator, 'locks')
    delete (navigator as { locks?: unknown }).locks
    try {
      const { hook, onSteerAction, press } = setup({ messages: [steerRow('m-1')] })
      expect(hook.result.current.offered).toBe(0)
      expect(hook.result.current.reachable).toBe(0)
      expect(press()).toBe(false)
      expect(onSteerAction).not.toHaveBeenCalled()
    } finally {
      if (original) Object.defineProperty(navigator, 'locks', original)
    }
  })

  it('ignores an answer for the old chat while the new chat has its own take-back in flight', async () => {
    const { hook, onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1')] })
    press()
    update({ slot: 'chat-b', messages: [steerRow('m-7'), steerRow('m-8')] })
    press()
    expect(onSteerAction).toHaveBeenLastCalledWith('m-8', 'edit', EDIT)
    await answer(0, 'accepted')
    expect(hook.result.current.offered).toBe(1)
    await answer(1, 'accepted')
    update({ messages: [steerRow('m-7'), steerRow('m-8', { steerState: 'retracted' })] })
    expect(hook.result.current.offered).toBe(1)
    expect(onSteerAction).toHaveBeenCalledTimes(2)
  })

  it('forgets every press when the next turn starts', async () => {
    const { hook, onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')], runEpoch: 3 })
    press()
    press()
    update({ runEpoch: 4 })
    await answer(0, 'accepted')
    update({ pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(hook.result.current.offered).toBe(2)
  })

  it('drops a kept press when a hard stop starts before the echo', () => {
    const { onSteerAction, update, press } = setup({ messages: [bubble('s-1')] })
    press()
    update({ stopState: 'killing' })
    update({ messages: [steerRow('m-9', { sendId: 's-1' })] })
    expect(onSteerAction).not.toHaveBeenCalled()
  })

  it('keeps a press made before the steer echo arrived, and sends it once the row has an id', () => {
    const { hook, onSteerAction, update, press } = setup({ messages: [bubble('s-1')] })
    expect(hook.result.current.offered).toBe(0)
    expect(press()).toBe(true)
    expect(onSteerAction).not.toHaveBeenCalled()

    update({ messages: [steerRow('m-9', { sendId: 's-1' })] })
    expect(onSteerAction).toHaveBeenCalledWith('m-9', 'edit', EDIT)
  })

  it.each([
    ['carries no take-back mark', { steerRetract: undefined }],
    ['refuses a take-back', { steerRetract: false }],
  ])('drops a kept press when the echoed row %s', (_case, meta) => {
    const { onSteerAction, update, press } = setup({ messages: [bubble('s-1')] })
    press()
    update({ messages: [steerRow('m-9', { sendId: 's-1', ...meta })] })
    expect(onSteerAction).not.toHaveBeenCalled()
  })

  it('takes back in send order behind a kept press', async () => {
    const { onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), bubble('s-2')] })
    expect([press(), press()]).toEqual([true, true])
    expect(onSteerAction).not.toHaveBeenCalled()

    update({ messages: [steerRow('m-1'), steerRow('m-2', { sendId: 's-2' })] })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-2', 'edit', EDIT)
    update({ pending: { 'm-2': 'edit' } })
    await answer(0, 'accepted')
    update({ messages: [steerRow('m-1'), steerRow('m-2', { sendId: 's-2', steerState: 'retracted' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-1', 'edit', EDIT)
  })

  it('skips a queued steer the turn read before its turn came', async () => {
    const { onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    update({ pending: { 'm-2': 'edit' } })
    press()
    await answer(0, 'accepted')
    update({ messages: [steerRow('m-1', { steerState: 'consumed' }), steerRow('m-2', { steerState: 'retracted' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
  })

  it('holds a queued press while the transcript is still loading, and sends it once the row is back', async () => {
    const { onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    update({ pending: { 'm-2': 'edit' } })
    press()
    update({ transcriptAuthoritative: false, messages: [] })
    await answer(0, 'accepted')
    update({ pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    update({ transcriptAuthoritative: true, messages: [steerRow('m-1'), steerRow('m-2', { steerState: 'retracted' })] })
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-1', 'edit', EDIT)
  })

  it('sends nothing for a queued press once the composer is gone', async () => {
    const { hook, onSteerAction, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    press()
    hook.unmount()
    await answer(0, 'accepted')
    expect(onSteerAction).toHaveBeenCalledTimes(1)
  })

  it('forgets every press when the turn ends', async () => {
    const { hook, onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    press()
    update({ slotRunning: false })
    await answer(0, 'accepted')
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(hook.result.current.offered).toBe(0)
  })

  it('forgets every press when the composer moves to another chat', async () => {
    const { hook, onSteerAction, update, answer, press } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    press()
    press()
    update({ slot: 'chat-b', messages: [steerRow('m-7')] })
    await answer(0, 'accepted')
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(hook.result.current.offered).toBe(1)
  })

  it('takes nothing back while a hard stop runs or with no turn running', () => {
    const killing = setup({ messages: [steerRow('m-1')], stopState: 'killing' })
    expect(killing.hook.result.current.offered).toBe(0)
    expect(killing.press()).toBe(false)

    const idle = setup({ messages: [steerRow('m-1')], slotRunning: false })
    expect(idle.press()).toBe(false)
    expect(killing.onSteerAction).not.toHaveBeenCalled()
    expect(idle.onSteerAction).not.toHaveBeenCalled()
  })
})
