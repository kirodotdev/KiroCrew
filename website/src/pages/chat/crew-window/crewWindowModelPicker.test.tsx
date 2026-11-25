/**
 * The crew window's effort-capability read: a dropped read is ridden out before
 * the window says it could not verify the options, and a 404 (a peer slot not
 * registered yet) is not a failure.
 */
import { describe, it, expect, vi, beforeEach, onTestFinished } from 'vitest'
import type { ReactNode } from 'react'
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../../../api/client', () => ({
  api: { crewPeerGet: vi.fn() },
}))

import { api } from '../../../api/client'
import { ApiError } from '../../../api/apiError'
import { useCrewWindowModelPicker } from './crewWindowModelPicker'

const peerGet = vi.mocked(api.crewPeerGet)

function renderPicker() {
  // No backoff, so the hook's own retry runs at once; nothing else retries.
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
  const seen: boolean[] = []
  const view = renderHook(() => {
    const picker = useCrewWindowModelPicker({
      instanceId: 'peer-1',
      slotKey: 'crew-window:peer-1:s1',
      slotPath: 'api/chat/slots/s1',
      slot: { model: 'claude-opus-5-5' },
      caps: undefined,
      enabled: true,
      onWritten: vi.fn(),
    })
    seen.push(picker.effortCapsFailed)
    return picker
  }, { wrapper })
  return { ...view, seen, client }
}

const capsKey = ['crew-window', 'peer-1', 'api/chat/slots/s1', 'selection-capabilities', 'claude-opus-5-5', '']

describe('useCrewWindowModelPicker effort-capability read', () => {
  beforeEach(() => { peerGet.mockReset() })

  it('rides out a dropped read without flagging the effort options', async () => {
    peerGet.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    peerGet.mockResolvedValue({ known: true, effort_supported: true, effort_levels: ['low', 'high'] })
    const { result, seen } = renderPicker()

    await waitFor(() => expect(peerGet).toHaveBeenCalledTimes(2), { timeout: 1000 })
    await waitFor(() => expect(result.current.chipProps).toBeTruthy())
    expect(seen).not.toContain(true)
  })

  it('refreshes a known answer after 30 seconds', async () => {
    vi.useFakeTimers()
    onTestFinished(() => { vi.useRealTimers() })
    peerGet.mockResolvedValue({ known: true, effort_supported: true, effort_levels: ['low', 'high'] })
    const { client } = renderPicker()

    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(client.getQueryState(capsKey)?.status).toBe('success')
    expect(peerGet).toHaveBeenCalledTimes(1)

    await act(async () => { await vi.advanceTimersByTimeAsync(29_999) })
    expect(peerGet).toHaveBeenCalledTimes(1)

    await act(async () => { await vi.advanceTimersByTimeAsync(1) })
    expect(peerGet).toHaveBeenCalledTimes(2)
  })

  it('does not treat a 404 (peer slot not registered yet) as a failed read', async () => {
    peerGet.mockRejectedValue(new ApiError(404, 'slot not found'))
    const { result, client } = renderPicker()

    await waitFor(() => expect(client.getQueryState(capsKey)?.status).toBe('error'))
    expect(result.current.effortCapsFailed).toBe(false)
  })

  it('still flags a real fault', async () => {
    peerGet.mockRejectedValue(new ApiError(500, 'boom'))
    const { result } = renderPicker()

    await waitFor(() => expect(result.current.effortCapsFailed).toBe(true))
  })
})

describe('a picked level the peer model does not offer (#42115)', () => {
  beforeEach(() => { peerGet.mockReset() })

  it('names the level that runs on the chip and keeps the pick', async () => {
    peerGet.mockResolvedValue({
      known: true, effort_supported: true, effort_levels: ['low', 'high'],
      effort_unapplied: 'max', effort_running: 'high',
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const { result } = renderHook(() => useCrewWindowModelPicker({
      instanceId: 'peer-1',
      slotKey: 'crew-window:peer-1:s1',
      slotPath: 'api/chat/slots/s1',
      slot: { model: 'claude-opus-5-5', reasoning_effort: 'max' },
      caps: undefined,
      enabled: true,
      onWritten: vi.fn(),
    }), { wrapper })

    expect(result.current.chipProps.reasoningEffort).toBe('max')
    await waitFor(() => expect(result.current.chipProps.reasoningEffort).toBe('high'))
    expect(result.current.chipProps.effortNotOffered).toBe('max')
  })

  it('uses the server pair when the peer slot has no effort pick', async () => {
    peerGet.mockResolvedValue({
      known: true, effort_supported: true, effort_levels: ['low', 'high'],
      effort_unapplied: 'max', effort_running: 'high', effort_inherited: true,
    })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const { result } = renderHook(() => useCrewWindowModelPicker({
      instanceId: 'peer-1',
      slotKey: 'crew-window:peer-1:s1',
      slotPath: 'api/chat/slots/s1',
      slot: { model: 'claude-opus-5-5' },
      caps: undefined,
      enabled: true,
      onWritten: vi.fn(),
    }), { wrapper })

    await waitFor(() => expect(result.current.chipProps.reasoningEffort).toBe('high'))
    expect(result.current.chipProps.effortNotOffered).toBe('max')
  })

  it('refetches the pair when the peer clears its effort pick', async () => {
    peerGet
      .mockResolvedValueOnce({
        known: true, effort_supported: true, effort_levels: ['low', 'high'],
        effort_unapplied: 'max', effort_running: 'high',
      })
      .mockResolvedValueOnce({ known: true, effort_supported: true, effort_levels: ['low', 'high'] })
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const { result, rerender } = renderHook(
      ({ reasoningEffort }: { reasoningEffort?: string }) => useCrewWindowModelPicker({
        instanceId: 'peer-1',
        slotKey: 'crew-window:peer-1:s1',
        slotPath: 'api/chat/slots/s1',
        slot: { model: 'claude-opus-5-5', reasoning_effort: reasoningEffort },
        caps: undefined,
        enabled: true,
        onWritten: vi.fn(),
      }),
      { wrapper, initialProps: { reasoningEffort: 'max' } },
    )

    await waitFor(() => expect(result.current.chipProps.reasoningEffort).toBe('high'))
    expect(result.current.chipProps.effortNotOffered).toBe('max')

    rerender({ reasoningEffort: undefined })

    await waitFor(() => expect(peerGet).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(client.getQueryState(capsKey)?.status).toBe('success'))
    expect(result.current.chipProps.reasoningEffort).toBe('')
    expect(result.current.chipProps.effortNotOffered).toBe('')
  })
})

describe('the effort slider while a new pick is read', () => {
  beforeEach(() => { peerGet.mockReset() })

  /** A peer GET the test answers itself. */
  function held() {
    let answer!: (v: unknown) => void
    peerGet.mockReturnValueOnce(new Promise(r => { answer = r }))
    return (v: unknown) => answer(v)
  }

  function renderSlot(initial: { model: string; reasoningEffort?: string }) {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
    const view = renderHook(
      ({ model, reasoningEffort }: { model: string; reasoningEffort?: string }) => useCrewWindowModelPicker({
        instanceId: 'peer-1',
        slotKey: 'crew-window:peer-1:s1',
        slotPath: 'api/chat/slots/s1',
        slot: { model, reasoning_effort: reasoningEffort },
        caps: undefined,
        enabled: true,
        onWritten: vi.fn(),
      }),
      { wrapper, initialProps: initial },
    )
    return { ...view, client }
  }

  const unappliedMax = {
    known: true, effort_supported: true, effort_levels: ['low', 'high'],
    effort_unapplied: 'max', effort_running: 'high',
  }

  it('keeps the slider mounted until the peer answers for the new level', async () => {
    peerGet.mockResolvedValueOnce(unappliedMax)
    const answer = held()
    const { result, rerender } = renderSlot({ model: 'claude-opus-5-5', reasoningEffort: 'max' })
    await waitFor(() => expect(result.current.chipProps.reasoningEffort).toBe('high'))

    rerender({ model: 'claude-opus-5-5', reasoningEffort: 'high' })
    await waitFor(() => expect(peerGet).toHaveBeenCalledTimes(2))
    expect(result.current.chipProps.hasEffort).toBe(true)
    expect(result.current.chipProps.reasoningEffort).toBe('high')
    expect(result.current.chipProps.effortNotOffered).toBe('')

    answer({
      known: true, effort_supported: true, effort_levels: ['low', 'medium'],
      effort_unapplied: 'high', effort_running: 'medium',
    })
    await waitFor(() => expect(result.current.chipProps.reasoningEffort).toBe('medium'))
    expect(result.current.chipProps.effortNotOffered).toBe('high')
    expect(result.current.chipProps.hasEffort).toBe(true)
  })

  it('does not name the previous level as running after the pick is cleared', async () => {
    peerGet.mockResolvedValueOnce(unappliedMax)
    const answer = held()
    const { result, rerender, client } = renderSlot({ model: 'claude-opus-5-5', reasoningEffort: 'max' })
    await waitFor(() => expect(result.current.chipProps.reasoningEffort).toBe('high'))

    rerender({ model: 'claude-opus-5-5', reasoningEffort: undefined })
    await waitFor(() => expect(peerGet).toHaveBeenCalledTimes(2))
    expect(result.current.chipProps.hasEffort).toBe(true)
    expect(result.current.chipProps.reasoningEffort).toBe('')
    expect(result.current.chipProps.effortNotOffered).toBe('')

    answer({ known: true, effort_supported: true, effort_levels: ['low', 'high'] })
    await waitFor(() => expect(client.getQueryState(capsKey)?.status).toBe('success'))
  })

  it('does not carry the old model answer over to a new model', async () => {
    peerGet.mockResolvedValueOnce(unappliedMax)
    const answerHigh = held()
    const answerOther = held()
    const { result, rerender, client } = renderSlot({ model: 'claude-opus-5-5', reasoningEffort: 'max' })
    await waitFor(() => expect(result.current.chipProps.hasEffort).toBe(true))

    // A model change while a same-model placeholder is showing asks again.
    rerender({ model: 'claude-opus-5-5', reasoningEffort: 'high' })
    await waitFor(() => expect(peerGet).toHaveBeenCalledTimes(2))
    expect(result.current.chipProps.hasEffort).toBe(true)

    rerender({ model: 'model-x', reasoningEffort: 'high' })
    await waitFor(() => expect(peerGet).toHaveBeenCalledTimes(3))
    expect(result.current.chipProps.hasEffort).toBe(false)

    answerOther({ known: true, effort_supported: false, effort_levels: [] })
    answerHigh({ known: true, effort_supported: true, effort_levels: ['low', 'high'] })
    const otherKey = ['crew-window', 'peer-1', 'api/chat/slots/s1', 'selection-capabilities', 'model-x', 'high']
    await waitFor(() => expect(client.getQueryState(otherKey)?.status).toBe('success'))
    expect(result.current.chipProps.hasEffort).toBe(false)
  })
})

/** Opens the picker for *slot* and renders its dropdown, as the crew window does. */
function OpenPicker({ slot, effortLevels }: { slot: { model?: string; served_model?: string }; effortLevels: string[] }) {
  const picker = useCrewWindowModelPicker({
    instanceId: 'peer-1',
    slotKey: 'crew-window:peer-1:s1',
    slotPath: 'api/chat/slots/s1',
    slot,
    caps: {
      instance_id: 'peer-1', version: '', local_version: '', version_match: true,
      agents: [], default_agent: '', models: [], effort_levels: effortLevels,
      workspaces: [], default_workspace: '', unavailable: {},
    },
    enabled: true,
    onWritten: vi.fn(),
  })
  return (
    <>
      <button type="button" onClick={e => picker.chipProps.onModelClick(e.currentTarget.getBoundingClientRect(), e.currentTarget)}>
        model
      </button>
      {picker.portal}
    </>
  )
}

function renderOpen(slot: { model?: string; served_model?: string }, effortLevels = ['low', 'high']) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, retryDelay: 0 } } })
  render(<QueryClientProvider client={client}><OpenPicker slot={slot} effortLevels={effortLevels} /></QueryClientProvider>)
  fireEvent.click(screen.getByRole('button', { name: 'model' }))
  return client
}

/** The picker's selection-capability read for *model*, as its query keys it. */
function selectionStatus(client: QueryClient, model: string) {
  return client.getQueryState(['crew-window', 'peer-1', 'api/chat/slots/s1', 'selection-capabilities', model, ''])?.status
}

describe('the crew window model picker on auto', () => {
  beforeEach(() => { peerGet.mockReset() })

  it('says effort does not apply, as the pane and composer pickers do', async () => {
    peerGet.mockResolvedValue({ known: true, effort_supported: false, effort_levels: [] })
    renderOpen({ model: '' })

    expect(await screen.findByTestId('effort-off-on-auto')).toBeTruthy()
  })

  // Both answers below say effort_supported: false, so the absence is read on
  // the settled render, not one an effort slider could replace.
  it('says nothing on a named model', async () => {
    peerGet.mockResolvedValue({ known: true, effort_supported: false, effort_levels: [] })
    const client = renderOpen({ model: 'model-x' })

    await waitFor(() => expect(selectionStatus(client, 'model-x')).toBe('success'))
    expect(screen.queryByTestId('effort-off-on-auto')).toBeNull()
  })

  it('says nothing when the peer provider takes no effort at all', async () => {
    peerGet.mockResolvedValue({ known: true, effort_supported: false, effort_levels: [] })
    const client = renderOpen({ model: '' }, [])

    await waitFor(() => expect(selectionStatus(client, '')).toBe('success'))
    expect(screen.queryByTestId('effort-off-on-auto')).toBeNull()
  })
})
