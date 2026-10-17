/**
 * A follow-up chip's send takes the same busy decision as the composer's Send.
 *
 * `sendFollowUp` used to hand every chip straight to `onFollowUpSend`, the plain
 * send, so a busy slot queued the chip even when its busy-send mode was Steer.
 * These pin the decision table against `fireComposer`'s default (no chord).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook } from '@testing-library/react'
import { useComposerSend } from '../components/chat-input/busySend'
import { BUSY_SEND_MODE_LS_KEY } from '../components/BusySendButton'
import type { ComposerBusyMode } from '../components/chat-input/props'

function setup(over: {
  isRunning?: boolean
  stopState?: 'idle' | 'soft_pending' | 'killing'
  jevAutoAvailable?: boolean
  busyMode?: ComposerBusyMode
} = {}) {
  const onSteer = vi.fn()
  const onSend = vi.fn()
  const onFollowUpSend = vi.fn()
  const { result } = renderHook(() => useComposerSend({
    slotId: 'slot-a',
    busyMode: over.busyMode ?? 'split',
    isRunning: over.isRunning ?? true,
    stopState: over.stopState,
    canSteer: true,
    onSteer,
    jevAutoAvailable: over.jevAutoAvailable ?? false,
    disabled: false,
    voiceTranscribing: false,
    value: '',
    pasteBlocks: [],
    pendingFilesCount: 0,
    pendingSessionsCount: 0,
    hasQuote: false,
    onSend,
    onFollowUpSend,
  }))
  return { send: result.current.sendFollowUp, onSteer, onSend, onFollowUpSend }
}

const storeMode = (mode: string) => localStorage.setItem(`${BUSY_SEND_MODE_LS_KEY}:slot-a`, mode)

beforeEach(() => localStorage.clear())

describe('sendFollowUp honours the busy-send mode', () => {
  it('sends normally when the slot is idle', () => {
    const { send, onSteer, onFollowUpSend } = setup({ isRunning: false })
    send('Deploy', 'row-1')
    expect(onFollowUpSend).toHaveBeenCalledWith('Deploy', 'row-1')
    expect(onSteer).not.toHaveBeenCalled()
  })

  it('steers the chip text when busy in the default Steer mode', () => {
    const { send, onSteer, onFollowUpSend } = setup()
    send('Deploy')
    expect(onSteer).toHaveBeenCalledWith({ text: 'Deploy' })
    expect(onFollowUpSend).not.toHaveBeenCalled()
  })

  it('queues when the slot is set to Queue', () => {
    storeMode('queue')
    const { send, onSteer, onFollowUpSend } = setup()
    send('Deploy')
    expect(onFollowUpSend).toHaveBeenCalledWith('Deploy', undefined)
    expect(onSteer).not.toHaveBeenCalled()
  })

  it('carries the auto flag in Auto mode while the seam is available', () => {
    storeMode('auto')
    const { send, onSteer } = setup({ jevAutoAvailable: true })
    send('Deploy')
    expect(onSteer).toHaveBeenCalledWith({ auto: true, text: 'Deploy' })
  })

  it('reads a stored Auto as a plain steer when the seam is unavailable', () => {
    storeMode('auto')
    const { send, onSteer } = setup({ jevAutoAvailable: false })
    send('Deploy')
    expect(onSteer).toHaveBeenCalledWith({ text: 'Deploy' })
  })

  it('steers on a steer-only surface whatever mode the slot stored', () => {
    storeMode('queue')
    const { send, onSteer, onFollowUpSend } = setup({ busyMode: 'steer-only' })
    send('Deploy')
    expect(onSteer).toHaveBeenCalledWith({ text: 'Deploy' })
    expect(onFollowUpSend).not.toHaveBeenCalled()
  })

  it('falls back to the plain send while a stop is pending, as Send does', () => {
    const { send, onSteer, onFollowUpSend } = setup({ stopState: 'soft_pending' })
    send('Deploy')
    expect(onFollowUpSend).toHaveBeenCalledWith('Deploy', undefined)
    expect(onSteer).not.toHaveBeenCalled()
  })

  it('steers the composer draft for a picked chip (no text of its own)', () => {
    const { send, onSteer } = setup()
    send(undefined)
    expect(onSteer).toHaveBeenCalledWith({})
  })
})
