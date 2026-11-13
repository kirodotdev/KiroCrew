/**
 * Regenerate on the answer to a gateway prompt (a workflow follow-up, a heartbeat
 * prompt) replaces only that answer. The prompt is an `inject` row, and the press
 * stops at it as it stops at a user row, so the rows above it are never marked for
 * replacement. Mirrors the scan in chat_regenerate.py.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ChatMessage } from '../types'

const calls = vi.hoisted(() => ({ regenerateSlot: vi.fn(async () => undefined) }))
vi.mock('../api/client', async (orig) => {
  const real = await orig<typeof import('../api/client')>()
  return { ...real, api: { ...real.api, regenerateSlot: calls.regenerateSlot } }
})

import { useRegenerate } from '../pages/chat/page/turnActions'

const row = (role: string, mid: string, meta: Record<string, unknown> = {}): ChatMessage =>
  ({ role, content: mid, cls: '', ts: mid, meta: { mid, ...meta } }) as unknown as ChatMessage

function press(messages: ChatMessage[]) {
  const { result } = renderHook(() => useRegenerate({
    activeSlot: 'slot-a', messages, messagesRef: { current: messages }, slotRunning: false,
    activeSlotRemoteBound: false, isStreaming: false, setRefusedPress: vi.fn(), showRefusedPress: vi.fn(),
  }))
  act(() => { result.current.handleRegenerate() })
  return result.current
}

beforeEach(() => { vi.clearAllMocks() })

describe('Regenerate on the answer to a gateway prompt', () => {
  it.each(['workflow', 'heartbeat'])('replaces only the answer below a %s prompt', (injectKind) => {
    const pressed = press([
      row('user', 'm-request'),
      row('assistant', 'm-result', { kind: 'workflow_result' }),
      row('inject', 'm-prompt', { injectKind }),
      row('assistant', 'm-answer'),
    ])
    expect([...pressed.replacingMids]).toEqual(['m-answer'])
    expect(calls.regenerateSlot).toHaveBeenCalledWith('slot-a')
  })

  it('does nothing while the prompt has no answer yet', () => {
    const pressed = press([
      row('user', 'm-request'),
      row('assistant', 'm-result', { kind: 'workflow_result' }),
      row('inject', 'm-prompt', { injectKind: 'workflow' }),
    ])
    expect(pressed.replacingMids.size).toBe(0)
    expect(calls.regenerateSlot).not.toHaveBeenCalled()
  })
})
