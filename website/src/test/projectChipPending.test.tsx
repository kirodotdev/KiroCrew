/**
 * A project pick waits on the turn-preparation lock (api_chat_slot_project
 * holds slot_switch_session_lock through session allocation), so on a cold
 * spawn the request can take seconds. The project chip shows a spinner and an
 * accessible status while the pick is in flight, on the chat it was made in
 * only, and clears it on success and on failure (#20351).
 */
import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, renderHook, screen } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'

import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { useSessionChoices } from '../pages/chat/page/sessionPicks'
import { api } from '../api/client'

type Deferred = { resolve: (v: { project: string }) => void; reject: (e: unknown) => void }

function barrier() {
  const held: Deferred[] = []
  vi.spyOn(api, 'chatSlotProject').mockImplementation(() => new Promise((resolve, reject) => { held.push({ resolve, reject }) }))
  return held
}

function mount(slot: string) {
  const dispatch = vi.fn()
  const hook = renderHook(({ activeSlot }) => useSessionChoices({
    dispatch: dispatch as never,
    queryClient: new QueryClient(),
    activeSlot,
    codexPairModels: false,
    setModelDropdown: vi.fn(),
    modelPickerReturnsFocusRef: { current: false },
    setPendingAgent: vi.fn(),
    setPendingModel: vi.fn(),
    setPendingProject: vi.fn(),
  }), { initialProps: { activeSlot: slot as string | null } })
  return { ...hook, dispatch }
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() })

beforeEach(() => { vi.spyOn(console, 'error').mockImplementation(() => {}) })
afterEach(() => { vi.restoreAllMocks() })

describe('project pick pending state', () => {
  it('is set while the route is held and clears on success with the stored project', async () => {
    const held = barrier()
    const { result, dispatch } = mount('s1')
    expect(result.current.projectSwitching).toBe(false)
    let done!: Promise<void>
    act(() => { done = result.current.setProject('/work/a') })
    await flush()
    expect(result.current.projectSwitching).toBe(true)
    await act(async () => { held[0].resolve({ project: '/real/work/a' }); await done })
    expect(result.current.projectSwitching).toBe(false)
    expect(dispatch).toHaveBeenCalledWith(expect.objectContaining({ payload: { key: 's1', project: '/real/work/a' } }))
  })

  it('clears on failure, writes no project, and uses the switch notice', async () => {
    const held = barrier()
    const { result, dispatch } = mount('s2')
    let done!: Promise<void>
    act(() => { done = result.current.setProject('/work/b') })
    await flush()
    expect(result.current.projectSwitching).toBe(true)
    await act(async () => { held[0].reject(new Error('nope')); await done })
    expect(result.current.projectSwitching).toBe(false)
    const types = dispatch.mock.calls.map(c => (c[0] as { type: string }).type)
    expect(types.some(t => /setAgentSwitchNotice/.test(t))).toBe(true)
    expect(dispatch.mock.calls.some(c => (c[0] as { payload?: { project?: string } }).payload?.project !== undefined)).toBe(false)
  })

  it('belongs to the chat it was made in, and repeated picks clear only after the last', async () => {
    const held = barrier()
    const { result, rerender } = mount('s3')
    let first!: Promise<void>
    let second!: Promise<void>
    act(() => { first = result.current.setProject('/work/c') })
    act(() => { second = result.current.setProject('/work/d') })
    await flush()
    expect(result.current.projectSwitching).toBe(true)
    rerender({ activeSlot: 'other' })
    expect(result.current.projectSwitching).toBe(false)
    rerender({ activeSlot: 's3' })
    expect(result.current.projectSwitching).toBe(true)
    await act(async () => { held[0].resolve({ project: '/work/c' }); await first })
    expect(result.current.projectSwitching).toBe(true)
    await flush()
    await act(async () => { held[held.length - 1].resolve({ project: '/work/d' }); await second })
    expect(result.current.projectSwitching).toBe(false)
  })
})

describe('project chip', () => {
  const base = { value: '', onChange: vi.fn(), onSend: vi.fn(), onProjectClick: vi.fn(), project: '/home/u/work/myrepo' }

  it('shows a spinner and announces the pending pick', () => {
    renderWithProviders(<ChatInput {...base} projectSwitching />)
    const chip = screen.getByTestId('composer-project-chip')
    expect(chip).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByTestId('composer-project-switching')).toBeInTheDocument()
    expect(screen.getByTestId('composer-project-switch-status')).toHaveTextContent('Switching project…')
    expect(chip).toHaveTextContent(/^Switching project…$/)
  })

  it('shows the folder icon and no status when nothing is pending', () => {
    renderWithProviders(<ChatInput {...base} />)
    expect(screen.getByTestId('composer-project-chip')).not.toHaveAttribute('aria-busy')
    expect(screen.queryByTestId('composer-project-switching')).toBeNull()
    expect(screen.getByTestId('composer-project-switch-status')).toHaveTextContent('')
    expect(screen.getByTestId('composer-project-chip')).toHaveTextContent(/^myrepo$/)
  })
})
