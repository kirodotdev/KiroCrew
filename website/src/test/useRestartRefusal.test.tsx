/**
 * A restart the gateway refuses after its 200 reaches the control that sent it:
 * the worker's `error` or `failed` step, seen after this attempt's `restarting`
 * step, calls `onRefused` with the step's detail.
 */
import type { ReactNode } from 'react'
import { describe, it, expect, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { setUpdateProgress } from '../store/dashboardSlice'
import { useRestartRefusal } from '../hooks/useRestartRefusal'

function setup(initial: { step: string; detail: string } | null = null) {
  const store = createTestStore()
  if (initial) store.dispatch(setUpdateProgress(initial))
  const onRefused = vi.fn()
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const { result } = renderHook(() => useRestartRefusal(onRefused), { wrapper })
  const push = (step: string, detail = '') => act(() => { store.dispatch(setUpdateProgress({ step, detail })) })
  return { result, onRefused, push }
}

describe('useRestartRefusal', () => {
  it.each(['error', 'failed'])('reports a %s step that follows this restart', (step) => {
    const { result, onRefused, push } = setup()
    act(() => result.current.begin())
    push('restarting', 'Restarting server…')
    push(step, 'Sessions did not drain in time.')
    expect(onRefused).toHaveBeenCalledTimes(1)
    expect(onRefused).toHaveBeenCalledWith('Sessions did not drain in time.')
  })

  it('ignores the previous attempt\'s failure still in the store', () => {
    const { result, onRefused } = setup({ step: 'error', detail: 'old failure' })
    act(() => result.current.begin())
    expect(onRefused).not.toHaveBeenCalled()
  })

  it('ignores a failure before this attempt\'s restarting step', () => {
    const { result, onRefused, push } = setup()
    act(() => result.current.begin())
    push('error', 'an update step failing, not this restart')
    expect(onRefused).not.toHaveBeenCalled()
  })

  it('does nothing before begin or after end', () => {
    const { result, onRefused, push } = setup()
    push('restarting')
    push('error', 'not watched')
    act(() => result.current.begin())
    act(() => result.current.end())
    push('restarting')
    push('error', 'ended')
    expect(onRefused).not.toHaveBeenCalled()
  })

  it('reports nothing, and does not throw, without the dashboard store', () => {
    const onRefused = vi.fn()
    const { result } = renderHook(() => useRestartRefusal(onRefused))
    act(() => result.current.begin())
    expect(onRefused).not.toHaveBeenCalled()
  })

  it('reports once per attempt', () => {
    const { result, onRefused, push } = setup()
    act(() => result.current.begin())
    push('restarting')
    push('error', 'first')
    push('restarting')
    push('failed', 'second')
    expect(onRefused).toHaveBeenCalledTimes(1)
    expect(onRefused).toHaveBeenCalledWith('first')
  })
})
