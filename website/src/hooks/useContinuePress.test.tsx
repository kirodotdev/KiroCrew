/**
 * The Resume press and its spinner, owned once for ChatPage and every pane
 * (#20692). The surfaces' own tests pin what each draws; this file pins the
 * press itself: which slot it posts to, what ends the spinner, and what a
 * refusal that comes back late does.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'

const continueSlot = vi.hoisted(() => vi.fn())
vi.mock('../api/client', () => ({ api: { continueSlot: (...a: unknown[]) => continueSlot(...a) } }))

import { useContinuePress } from './useContinuePress'

type Props = Parameters<typeof useContinuePress>[0]

let onRefused: ReturnType<typeof vi.fn>
const base = (): Props => ({ slot: 'slot-a', turnRunning: false, continuable: true, onRefused })

function mount(props: Props = base()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  return renderHook((p: Props) => useContinuePress(p), { initialProps: props, wrapper })
}

beforeEach(() => {
  continueSlot.mockReset()
  continueSlot.mockResolvedValue({ ok: true })
  onRefused = vi.fn()
})

afterEach(() => { vi.useRealTimers() })

describe('useContinuePress', () => {
  it('posts continue for its slot once, and holds the spinner until a turn runs', async () => {
    const hook = mount()
    act(() => { hook.result.current.press() })
    expect(hook.result.current.continuing).toBe(true)
    await waitFor(() => expect(continueSlot).toHaveBeenCalledTimes(1))
    expect(continueSlot).toHaveBeenCalledWith('slot-a')

    // The request settling is not the success signal; a second press is held off.
    act(() => { hook.result.current.press() })
    expect(continueSlot).toHaveBeenCalledTimes(1)
    expect(hook.result.current.continuing).toBe(true)

    hook.rerender({ ...base(), turnRunning: true })
    expect(hook.result.current.continuing).toBe(false)
  })

  it('does nothing without a slot or when a continuation is not safe', () => {
    const noSlot = mount({ ...base(), slot: null })
    act(() => { noSlot.result.current.press() })
    expect(noSlot.result.current.continuing).toBe(false)

    const unsafe = mount({ ...base(), continuable: false })
    act(() => { unsafe.result.current.press() })
    expect(unsafe.result.current.continuing).toBe(false)
    expect(continueSlot).not.toHaveBeenCalled()
  })

  it('hands a refusal to the surface and ends the spinner', async () => {
    const refusal = new Error('slot is running')
    continueSlot.mockRejectedValue(refusal)
    const hook = mount()
    act(() => { hook.result.current.press() })

    await waitFor(() => expect(onRefused).toHaveBeenCalledWith(refusal))
    await waitFor(() => expect(hook.result.current.continuing).toBe(false))
  })

  it('drops a refusal that comes back after the surface was pointed at another slot', async () => {
    let refuse: (e: Error) => void = () => {}
    continueSlot.mockImplementation(() => new Promise((_, reject) => { refuse = reject }))
    const hook = mount()
    act(() => { hook.result.current.press() })
    await waitFor(() => expect(continueSlot).toHaveBeenCalledWith('slot-a'))

    hook.rerender({ ...base(), slot: 'slot-b' })
    expect(hook.result.current.continuing).toBe(false)
    await act(async () => { refuse(new Error('slot is running')) })

    expect(onRefused).not.toHaveBeenCalled()
    expect(hook.result.current.continuing).toBe(false)
  })

  it('frees the spinner after 30 seconds if the press neither starts a turn nor is refused', () => {
    vi.useFakeTimers()
    const hook = mount()
    act(() => { hook.result.current.press() })
    expect(hook.result.current.continuing).toBe(true)

    act(() => { vi.advanceTimersByTime(29_999) })
    expect(hook.result.current.continuing).toBe(true)
    act(() => { vi.advanceTimersByTime(1) })
    expect(hook.result.current.continuing).toBe(false)
  })
})
