import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* Dictation provenance, from the Voice atom's side: every route a transcript
 * takes into a composer or a parked draft records which session now holds
 * spoken words, and a discard that takes the words back out takes the record
 * back with them. The send paths read this record (`dictationProvenance`), so
 * a route that forgot to record would send dictated text without the note,
 * and one that recorded for the wrong session would mark a typed message.
 *
 * The engine is faked so the test plays the recogniser: the delivery callback
 * (first argument) carries streaming finals and batch results alike, and
 * `onPartial` carries the live streaming hypothesis. */

type Delivery = (text: string, sessionId: string | null, origin: 'stream' | 'batch') => void
type Partial = (text: string, sessionId: string | null) => void

const fx = vi.hoisted(() => {
  const captured: { onText: unknown; onPartial: unknown } = { onText: null, onPartial: null }
  const engine = {
    recording: false, transcribing: false, sessionOwner: null as string | null, streamEnabled: false,
    transport: null as 'stream' | null, partial: '',
    toggle: () => {}, start: async () => {}, stop: () => {}, cancel: () => {}, prewarm: () => {},
    error: null, level: 0, deviceLabel: '', deviceId: '', clearError: () => {},
    download: null, sampleRef: { current: {} }, switchDevice: () => {}, deviceSwitchIsLive: false,
  }
  return { captured, engine }
})

vi.mock('../../hooks/useVoiceInput', () => ({
  useVoiceInput: (onText: unknown, opts: { onPartial?: unknown }) => {
    fx.captured.onText = onText
    fx.captured.onPartial = opts.onPartial
    return fx.engine
  },
  voiceInputSupported: true,
}))
vi.mock('../../hooks/usePushToTalk', () => ({ usePushToTalk: () => ({ config: { enabled: false, mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }, phase: 'idle', holding: false }) }))
vi.mock('../../api/client', () => ({
  api: { sttConfig: vi.fn().mockResolvedValue({ enabled: true, available: true, provider: 'local' }) },
}))

import { useComposerVoice, _resetMicOwner } from './useComposerVoice'
import { _resetDictationProvenance, isDictated, markDictated } from './dictationProvenance'

const STT = { enabled: true, available: true, streaming: false, dictation_panel: true, provider: 'local', dictation_note: true }

function mount(opts: { onScreen?: boolean; deliverOffScreen?: (target: string, append: (base: string) => string) => void; streaming?: boolean } = {}) {
  const inputRef = { current: '' }
  const slot = { current: 'slot-a' }
  fx.engine.streamEnabled = !!opts.streaming
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  qc.setQueryData(['sttConfig'], { ...STT, streaming: !!opts.streaming })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  const view = renderHook(
    () => useComposerVoice({
      sessionId: slot.current,
      inputRef,
      setInput: (v: string) => { inputRef.current = v },
      ...(opts.onScreen === false ? { isComposerFor: () => false } : {}),
      ...(opts.deliverOffScreen ? { deliverOffScreen: opts.deliverOffScreen } : {}),
    }),
    { wrapper },
  )
  const switchSlot = (id: string) => { slot.current = id; view.rerender() }
  return {
    view, inputRef, switchSlot,
    deliver: () => fx.captured.onText as Delivery,
    partial: () => fx.captured.onPartial as Partial,
  }
}

beforeEach(() => {
  _resetMicOwner()
  _resetDictationProvenance()
  fx.engine.transport = null
  fx.engine.partial = ''
  fx.engine.recording = false
})

describe('dictation provenance: recording what holds spoken words', () => {
  it('a batch transcript landing in the composer marks that session, and only that one', async () => {
    const { inputRef, deliver } = mount()
    expect(isDictated('slot-a')).toBe(false)

    await act(async () => { deliver()('hello there', 'slot-a', 'batch') })

    expect(inputRef.current).toBe('hello there')
    expect(isDictated('slot-a')).toBe(true)
    expect(isDictated('slot-b')).toBe(false)
  })

  it('a streaming hypothesis marks the session as soon as it is in the composer', async () => {
    const { view, inputRef, partial } = mount({ streaming: true })
    // A capture start arms the streaming routes (mounting disarms them).
    await act(async () => { await view.result.current.startVoice() })

    await act(async () => { partial()('turn the lights', 'slot-a') })

    expect(inputRef.current).toBe('turn the lights')
    expect(isDictated('slot-a')).toBe(true)
  })

  it('a batch transcript for a session that is off screen marks THAT session, not the one on screen', async () => {
    const parked: Record<string, string> = {}
    const { deliver } = mount({
      onScreen: false,
      deliverOffScreen: (target, append) => { parked[target] = append(parked[target] ?? '') },
    })

    await act(async () => { deliver()('for the other chat', 'slot-b', 'batch') })

    expect(parked['slot-b']).toBe('for the other chat')
    expect(isDictated('slot-b')).toBe(true)
    expect(isDictated('slot-a')).toBe(false)
  })

  it('an off-screen transcript with nowhere to go is dropped and marks nothing', async () => {
    const { deliver } = mount({ onScreen: false })

    await act(async () => { deliver()('lost words', 'slot-b', 'batch') })

    expect(isDictated('slot-b')).toBe(false)
  })

  it('a transcript that changes nothing does not mark a composer that holds only typing', async () => {
    const { inputRef, deliver } = mount()
    inputRef.current = 'typed'

    // An empty transcript leaves the composer as it was.
    await act(async () => { deliver()('', 'slot-a', 'batch') })

    expect(inputRef.current).toBe('typed')
    expect(isDictated('slot-a')).toBe(false)
  })

  it('switching slots leaves the record with the slot that was dictated into', async () => {
    const { deliver, switchSlot } = mount()
    await act(async () => { deliver()('first slot words', 'slot-a', 'batch') })

    switchSlot('slot-b')

    expect(isDictated('slot-a')).toBe(true)
    expect(isDictated('slot-b')).toBe(false)
  })
})

describe('dictation provenance: a discard takes back what it took out', () => {
  it('a hypothesis that retracts to nothing leaves a typed draft unmarked', async () => {
    const { view, inputRef, partial } = mount({ streaming: true })
    inputRef.current = 'typed by hand'
    await act(async () => { await view.result.current.startVoice() })

    await act(async () => { partial()('maybe', 'slot-a') })
    expect(inputRef.current).toBe('typed by hand maybe')
    expect(isDictated('slot-a')).toBe(true)

    await act(async () => { partial()('', 'slot-a') })
    expect(inputRef.current).toBe('typed by hand')
    expect(isDictated('slot-a')).toBe(false)
  })

  it('a hypothesis that retracts to nothing keeps a mark set before it began', async () => {
    const { view, inputRef, partial } = mount({ streaming: true })
    inputRef.current = 'spoken earlier'
    markDictated('slot-a')
    await act(async () => { await view.result.current.startVoice() })

    await act(async () => { partial()('maybe', 'slot-a') })
    await act(async () => { partial()('', 'slot-a') })
    expect(inputRef.current).toBe('spoken earlier')
    expect(isDictated('slot-a')).toBe(true)
  })

  it('discarding a streaming dictation into an untouched composer clears the mark it set', async () => {
    const { view, inputRef, partial } = mount({ streaming: true })
    await act(async () => { await view.result.current.startVoice() })
    fx.engine.transport = 'stream'
    fx.engine.recording = true
    view.rerender()

    await act(async () => { partial()('never mind', 'slot-a') })
    fx.engine.partial = 'never mind'
    view.rerender()
    expect(isDictated('slot-a')).toBe(true)

    act(() => { view.result.current.cancelVoice() })

    expect(inputRef.current).toBe('')
    expect(isDictated('slot-a')).toBe(false)
  })

  it('discarding a second dictation keeps the mark an earlier one in the same draft set', async () => {
    const { view, inputRef, partial } = mount({ streaming: true })
    // An earlier dictation already left words in this draft.
    inputRef.current = 'earlier words'
    markDictated('slot-a')
    await act(async () => { await view.result.current.startVoice() })
    fx.engine.transport = 'stream'
    fx.engine.recording = true
    view.rerender()

    await act(async () => { partial()('scrap this', 'slot-a') })
    fx.engine.partial = 'scrap this'
    view.rerender()

    act(() => { view.result.current.cancelVoice() })

    expect(inputRef.current).toBe('earlier words')
    expect(isDictated('slot-a')).toBe(true)
  })
})
