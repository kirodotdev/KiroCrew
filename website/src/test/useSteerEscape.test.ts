// The composer's Escape on pending steers (useSteerEscape): which row each press
// takes back, how presses queue behind the one take-back a chat runs at a time,
// and when the stop Escape asks for is sent.
import { describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ChatMessage } from '../types'
import type { SteerRetractRequestOutcome } from '../hooks/useSteerRetract'
import { useSteerEscape, type SteerEscapeOptions } from '../hooks/useSteerEscape'

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

function setup(initial: Partial<SteerEscapeOptions> = {}) {
  const answers: Array<(outcome: SteerRetractRequestOutcome) => void> = []
  const onSteerAction = vi.fn(() => new Promise<SteerRetractRequestOutcome>(resolve => { answers.push(resolve) }))
  const stop = vi.fn()
  const props: SteerEscapeOptions = {
    slot: 'chat-a',
    messages: [],
    slotRunning: true,
    onSteerAction,
    pending: {},
    stop,
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
  return { hook, onSteerAction, stop, update, answer }
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
    const { hook, onSteerAction } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    let verdict = ''
    act(() => { verdict = hook.result.current.takeBack() })
    expect(verdict).toBe('more')
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(onSteerAction).toHaveBeenCalledWith('m-2', 'edit', { prepend: true, retryBusy: true })
    expect(hook.result.current.offered).toBe(1)
  })

  it('queues the next press until the take-back before it settles, then sends it', async () => {
    const { hook, onSteerAction, update, answer } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    act(() => { hook.result.current.takeBack() })
    update({ pending: { 'm-2': 'edit' } })
    let verdict = ''
    act(() => { verdict = hook.result.current.takeBack() })
    expect(verdict).toBe('last')
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(hook.result.current.offered).toBe(0)

    await answer(0, 'accepted')
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    update({ messages: [steerRow('m-1'), steerRow('m-2', { steerState: 'retracted' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-1', 'edit', { prepend: true, retryBusy: true })
  })

  it('answers none when every pending steer is already asked for', () => {
    const { hook, update } = setup({ messages: [steerRow('m-1')] })
    act(() => { hook.result.current.takeBack() })
    update({ pending: { 'm-1': 'edit' } })
    let verdict = ''
    act(() => { verdict = hook.result.current.takeBack() })
    expect(verdict).toBe('none')
  })

  it('leaves a steer whose row control already started a take-back to that take-back', () => {
    const { hook, onSteerAction } = setup({ messages: [steerRow('m-1'), steerRow('m-2')], pending: { 'm-2': 'queue' } })
    expect(hook.result.current.offered).toBe(1)
    let verdict = ''
    act(() => { verdict = hook.result.current.takeBack() })
    expect(verdict).toBe('last')
    expect(onSteerAction).not.toHaveBeenCalled()
  })

  it('sends the stop at once when no take-back is queued', () => {
    const { hook, stop } = setup({ messages: [steerRow('m-1')] })
    act(() => { hook.result.current.stop() })
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('holds the stop until the take-back it follows is armed', async () => {
    const { hook, stop, answer } = setup({ messages: [steerRow('m-1')] })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.stop() })
    act(() => { hook.result.current.stop() })
    expect(stop).not.toHaveBeenCalled()
    await answer(0, 'accepted')
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('sends the stop when the steer was no longer pending to take back', async () => {
    const { hook, stop, answer } = setup({ messages: [steerRow('m-1')] })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.stop() })
    await answer(0, 'not_pending')
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it.each(['refused', 'unknown', 'not_sent'] as const)('drops the queue and the stop when a take-back is %s', async (outcome) => {
    const { hook, onSteerAction, stop, update, answer } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.stop() })
    await answer(0, outcome)
    update({ pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(stop).not.toHaveBeenCalled()
    expect(hook.result.current.offered).toBe(2)
  })

  it('keeps a press made before the steer echo arrived, and sends it once the row has an id', async () => {
    const { hook, onSteerAction, stop, update, answer } = setup({ messages: [bubble('s-1')] })
    expect(hook.result.current.offered).toBe(0)
    let verdict = ''
    act(() => { verdict = hook.result.current.takeBack() })
    expect(verdict).toBe('last')
    act(() => { hook.result.current.stop() })
    expect(onSteerAction).not.toHaveBeenCalled()

    update({ messages: [steerRow('m-9', { sendId: 's-1' })] })
    expect(onSteerAction).toHaveBeenCalledWith('m-9', 'edit', { prepend: true, retryBusy: true })
    expect(stop).not.toHaveBeenCalled()
    await answer(0, 'accepted')
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('drops a kept press when the echo shows the steer cannot be taken back, and sends the stop', () => {
    const { hook, onSteerAction, stop, update } = setup({ messages: [bubble('s-1')] })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.stop() })
    update({ messages: [steerRow('m-9', { sendId: 's-1', steerRetract: undefined })] })
    expect(onSteerAction).not.toHaveBeenCalled()
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('takes back in send order behind a kept press', async () => {
    const { hook, onSteerAction, update, answer } = setup({ messages: [steerRow('m-1'), bubble('s-2')] })
    let first = ''
    let second = ''
    act(() => { first = hook.result.current.takeBack() })
    act(() => { second = hook.result.current.takeBack() })
    expect([first, second]).toEqual(['more', 'last'])
    expect(onSteerAction).not.toHaveBeenCalled()

    update({ messages: [steerRow('m-1'), steerRow('m-2', { sendId: 's-2' })] })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-2', 'edit', { prepend: true, retryBusy: true })
    update({ pending: { 'm-2': 'edit' } })
    await answer(0, 'accepted')
    update({ messages: [steerRow('m-1'), steerRow('m-2', { sendId: 's-2', steerState: 'retracted' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(2)
    expect(onSteerAction).toHaveBeenLastCalledWith('m-1', 'edit', { prepend: true, retryBusy: true })
  })

  it('skips a queued steer the turn read before its turn came', async () => {
    const { hook, onSteerAction, stop, update, answer } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    act(() => { hook.result.current.takeBack() })
    update({ pending: { 'm-2': 'edit' } })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.stop() })
    await answer(0, 'accepted')
    update({ messages: [steerRow('m-1', { steerState: 'consumed' }), steerRow('m-2', { steerState: 'retracted' })], pending: {} })
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(stop).toHaveBeenCalledTimes(1)
  })

  it('forgets every press and the stop when the turn ends', async () => {
    const { hook, stop, update, answer } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.stop() })
    update({ slotRunning: false })
    await answer(0, 'accepted')
    expect(stop).not.toHaveBeenCalled()
    expect(hook.result.current.offered).toBe(0)
  })

  it('forgets every press and the stop when the composer moves to another chat', async () => {
    const { hook, onSteerAction, stop, update, answer } = setup({ messages: [steerRow('m-1'), steerRow('m-2')] })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.takeBack() })
    act(() => { hook.result.current.stop() })
    update({ slot: 'chat-b', messages: [steerRow('m-7')] })
    await answer(0, 'accepted')
    expect(stop).not.toHaveBeenCalled()
    expect(onSteerAction).toHaveBeenCalledTimes(1)
    expect(hook.result.current.offered).toBe(1)
  })

  it('takes nothing back while a hard stop runs or with no turn running', () => {
    const killing = setup({ messages: [steerRow('m-1')], stopState: 'killing' })
    expect(killing.hook.result.current.offered).toBe(0)
    let verdict = ''
    act(() => { verdict = killing.hook.result.current.takeBack() })
    expect(verdict).toBe('none')

    const idle = setup({ messages: [steerRow('m-1')], slotRunning: false })
    act(() => { verdict = idle.hook.result.current.takeBack() })
    expect(verdict).toBe('none')
    expect(killing.onSteerAction).not.toHaveBeenCalled()
    expect(idle.onSteerAction).not.toHaveBeenCalled()
  })
})
