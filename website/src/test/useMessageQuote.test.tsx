import { describe, it, expect, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useEffect } from 'react'
import { useMessageQuote } from '../chat-core/composer/useMessageQuote'
import type { MessageQuote } from '../chat-core/composer/messageQuote'
import { quoteBlock } from '../chat-core/composer/messageQuote'

describe('useMessageQuote', () => {
  it('stages a message, reveals the composer, and replaces rather than stacks', () => {
    const reveal = vi.fn()
    const { result } = renderHook(() => useMessageQuote({ slot: 'chat-1', revealComposer: reveal }))
    act(() => result.current.quoteMessage('assistant', 'first', 't1', 'm1'))
    expect(result.current.pendingQuote).toEqual({ role: 'assistant', text: 'first', ts: 't1', mid: 'm1' })
    expect(reveal).toHaveBeenCalledTimes(1)
    act(() => result.current.quoteMessage('user', 'second', 't2'))
    expect(result.current.pendingQuote).toEqual({ role: 'user', text: 'second', ts: 't2' })
  })

  it('stamps the surface speaker name on assistant quotes only', () => {
    const { result } = renderHook(() => useMessageQuote({ slot: 'chat-1', assistantName: 'Worker' }))
    act(() => result.current.quoteMessage('assistant', 'a', 't1'))
    expect(result.current.pendingQuote?.author).toBe('Worker')
    act(() => result.current.quoteMessage('user', 'u', 't2'))
    expect(result.current.pendingQuote?.author).toBeUndefined()
  })

  it('ignores a row with nothing to quote', () => {
    const { result } = renderHook(() => useMessageQuote({ slot: 'chat-1' }))
    act(() => result.current.quoteMessage('user', '   '))
    expect(result.current.pendingQuote).toBeNull()
  })

  it('consume hands back the record with the block prepended and clears the stage', () => {
    const { result } = renderHook(() => useMessageQuote({ slot: 'chat-1' }))
    act(() => result.current.quoteMessage('assistant', 'quoted', 't1'))
    let out: ReturnType<typeof result.current.consume> | undefined
    act(() => { out = result.current.consume('typed') })
    expect(out!.quote).toEqual({ role: 'assistant', text: 'quoted', ts: 't1' })
    expect(out!.text).toBe(quoteBlock(out!.quote!) + '\n\ntyped')
    expect(result.current.pendingQuote).toBeNull()
  })

  it('consume with nothing staged returns the text untouched', () => {
    const { result } = renderHook(() => useMessageQuote({ slot: 'chat-1' }))
    let out: ReturnType<typeof result.current.consume> | undefined
    act(() => { out = result.current.consume('typed') })
    expect(out).toEqual({ quote: null, text: 'typed' })
  })

  it('restage never replaces a quote staged while the send was in flight; recoverInto puts it in the text instead', () => {
    const { result } = renderHook(() => useMessageQuote({ slot: 'chat-1' }))
    act(() => result.current.quoteMessage('assistant', 'A', 't1'))
    let taken: ReturnType<typeof result.current.consume> | undefined
    act(() => { taken = result.current.consume('why?') })
    act(() => result.current.quoteMessage('user', 'B', 't2'))
    let took: boolean | undefined
    act(() => { took = result.current.restage(taken!.quote) })
    expect(took).toBe(false)
    expect(result.current.pendingQuote?.text).toBe('B')
    let text = ''
    act(() => { text = result.current.recoverInto('why?', taken!.quote) })
    expect(text).toBe(quoteBlock(taken!.quote!) + '\n\nwhy?')
    expect(result.current.pendingQuote?.text).toBe('B')
  })

  it('recoverInto restages onto a free stage and leaves the text alone; a non-live composer gets the block', () => {
    const { result } = renderHook(() => useMessageQuote({ slot: 'chat-1' }))
    const q = { role: 'assistant' as const, text: 'A', ts: 't1' }
    let text = ''
    act(() => { text = result.current.recoverInto('why?', q) })
    expect(text).toBe('why?')
    expect(result.current.pendingQuote).toEqual(q)
    act(() => result.current.clearQuote())
    act(() => { text = result.current.recoverInto('why?', q, false) })
    expect(text).toBe(quoteBlock(q) + '\n\nwhy?')
    expect(result.current.pendingQuote).toBeNull()
    expect(result.current.recoverInto('why?', null)).toBe('why?')
  })

  it('restage puts a consumed quote back (a failed send); null is a no-op', () => {
    const { result } = renderHook(() => useMessageQuote({ slot: 'chat-1' }))
    act(() => result.current.quoteMessage('user', 'q', 't1'))
    let taken: ReturnType<typeof result.current.consume> | undefined
    act(() => { taken = result.current.consume('') })
    expect(result.current.pendingQuote).toBeNull()
    act(() => result.current.restage(taken!.quote))
    expect(result.current.pendingQuote).toEqual({ role: 'user', text: 'q', ts: 't1' })
    act(() => result.current.clearQuote())
    act(() => result.current.restage(null))
    expect(result.current.pendingQuote).toBeNull()
  })

  it('a send issued by an effect on the switch commit consumes nothing from the old slot', () => {
    // `consume` reads the ref, and an effect of the SAME commit that switched
    // the slot (an armed auto-send) may call it before any state update lands.
    let seen: MessageQuote | null | undefined
    const { result, rerender } = renderHook(({ slot }: { slot: string }) => {
      const hook = useMessageQuote({ slot })
      useEffect(() => { if (slot === 'chat-2' && seen === undefined) seen = hook.consume('typed').quote }, [slot, hook])
      return hook
    }, { initialProps: { slot: 'chat-1' } })
    act(() => result.current.quoteMessage('user', 'private to chat-1'))
    rerender({ slot: 'chat-2' })
    expect(seen).toBeNull()
    expect(result.current.pendingQuote).toBeNull()
  })

  it('drops the stage on a slot switch, not on mount', () => {
    const { result, rerender } = renderHook(({ slot }: { slot: string | null }) => useMessageQuote({ slot }), { initialProps: { slot: 'chat-1' } })
    act(() => result.current.quoteMessage('user', 'q'))
    rerender({ slot: 'chat-1' })
    expect(result.current.pendingQuote).not.toBeNull()
    rerender({ slot: 'chat-2' })
    expect(result.current.pendingQuote).toBeNull()
  })
})
