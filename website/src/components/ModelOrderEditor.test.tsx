/**
 * Settings ▸ Chat model-order drag list.
 *
 * A pointer drag cannot be faithfully simulated in jsdom (it needs real
 * PointerEvents plus layout measurement), so this stubs the DndContext, captures
 * the card's real `onDragEnd`, and invokes it — exercising the persisted-order
 * computation rather than the gesture. Mirrors the pattern in
 * `test/ChatSidebar.dragFreezeOrder.test.tsx`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// Capture the real onDragEnd from the stubbed DndContext (children pass through),
// keeping every other @dnd-kit/core export — sensors, DragOverlay — real.
const dnd = vi.hoisted(() => ({ onDragEnd: undefined as ((e: unknown) => void) | undefined }))
vi.mock('@dnd-kit/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@dnd-kit/core')>()
  return {
    ...actual,
    DndContext: (props: { children?: React.ReactNode; onDragEnd?: (e: unknown) => void }) => {
      dnd.onDragEnd = props.onDragEnd
      return props.children as React.ReactElement
    },
  }
})

const { patchConfigMock, kirocrewConfigMock, modelsMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({ agent: { model_order: [] as string[] } })),
  modelsMock: vi.fn(() => [
    { name: 'auto', description: '' },
    { name: 'opus', description: 'Opus' },
    { name: 'sonnet', description: 'Sonnet' },
    { name: 'haiku', description: 'Haiku' },
  ]),
}))

vi.mock('../api/client', () => ({
  api: { kirocrewConfig: kirocrewConfigMock, patchConfig: patchConfigMock },
}))

vi.mock('../hooks/useAvailableModels', () => ({
  // The order-load-failure flag the hosts now render; false = not failed.
  useModelOrderLoadFailed: () => false,
  useAvailableModels: () => modelsMock(),
}))

import { ModelOrderEditor, orderModelNames } from './ModelOrderEditor'

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ModelOrderEditor />
    </QueryClientProvider>,
  )
}

const RESET = /Reset to default order/

describe('ModelOrderEditor', () => {
  it('stays read-only while the config read is pending, and never writes', async () => {
    // A config read that never resolves: cached models + pending config is the
    // overwrite hazard the gate closes — an enabled drag here would persist an
    // order derived from the [] fallback over the user's saved one.
    kirocrewConfigMock.mockImplementation(() => new Promise(() => {}))
    wrap()
    expect(await screen.findByText('opus')).toBeInTheDocument()
    // Read-only branch: no drag surface, no reset action.
    expect(dnd.onDragEnd).toBeUndefined()
    expect(screen.queryByText(RESET)).not.toBeInTheDocument()
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('surfaces a failed config read and stays read-only', async () => {
    kirocrewConfigMock.mockImplementation(() => Promise.reject(new Error('boom')))
    wrap()
    expect(await screen.findByText(/Couldn't load the saved model order/)).toBeInTheDocument()
    expect(dnd.onDragEnd).toBeUndefined()
    expect(screen.queryByText(RESET)).not.toBeInTheDocument()
  })

  beforeEach(() => {
    patchConfigMock.mockClear()
    dnd.onDragEnd = undefined
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent: { model_order: [] } }))
    modelsMock.mockReturnValue([
      { name: 'auto', description: '' },
      { name: 'opus', description: 'Opus' },
      { name: 'sonnet', description: 'Sonnet' },
      { name: 'haiku', description: 'Haiku' },
    ])
  })

  it('persists the FULL reordered array on drop', async () => {
    wrap()
    // Rows render (a missed DndContext capture would make the drag a silent no-op).
    expect(await screen.findByText('opus')).toBeInTheDocument()
    // Wait for the DRAG surface, not just a row: the read-only branch renders
    // the same names while ['kirocrewConfig'] is pending (the config gate), so
    // a row appearing does not yet mean DndContext mounted.
    await waitFor(() => expect(typeof dnd.onDragEnd).toBe('function'))
    // Drag haiku (last) onto opus (first): [opus, sonnet, haiku] -> [haiku, opus, sonnet].
    act(() => dnd.onDragEnd!({ active: { id: 'haiku' }, over: { id: 'opus' } }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.model_order', ['haiku', 'opus', 'sonnet']),
    )
  })

  it('a drag preserves a saved id the live list no longer advertises', async () => {
    // Saved order carries 'retired-model', which the live list (opus, sonnet,
    // haiku) does not serve. The drag surface must not render it — and the
    // write after a drag must KEEP it, in its saved slot, per the write-side
    // contract (a stale id survives; kiro renames models).
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ agent: { model_order: ['opus', 'retired-model', 'sonnet'] } }))
    wrap()
    expect(await screen.findByText('opus')).toBeInTheDocument()
    expect(screen.queryByText('retired-model')).not.toBeInTheDocument()
    await waitFor(() => expect(typeof dnd.onDragEnd).toBe('function'))
    // Drag sonnet onto opus: live render [opus, sonnet, haiku] -> [sonnet, opus, haiku].
    act(() => dnd.onDragEnd!({ active: { id: 'sonnet' }, over: { id: 'opus' } }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith(
        'agent.model_order',
        ['sonnet', 'retired-model', 'opus', 'haiku'],
      ),
    )
  })

  it('writes [] on reset to default order', async () => {
    wrap()
    fireEvent.click(await screen.findByRole('button', { name: RESET }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.model_order', []))
  })

  it('does not write when a row is dropped on itself or nowhere', async () => {
    wrap()
    await screen.findByText('opus')
    // Same config-gate wait as the drop test: the handler only exists once the
    // drag surface mounts, after ['kirocrewConfig'] resolves.
    await waitFor(() => expect(typeof dnd.onDragEnd).toBe('function'))
    act(() => dnd.onDragEnd!({ active: { id: 'opus' }, over: { id: 'opus' } }))
    act(() => dnd.onDragEnd!({ active: { id: 'opus' }, over: null }))
    expect(patchConfigMock).not.toHaveBeenCalled()
  })

  it('reorders from a persisted saved order', async () => {
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent: { model_order: ['haiku', 'sonnet', 'opus'] } }))
    wrap()
    // displayNames resolves to the saved order; drag opus (last) up onto haiku (first).
    await waitFor(() => expect(kirocrewConfigMock).toHaveBeenCalled())
    await screen.findByText('opus')
    await waitFor(() => expect(typeof dnd.onDragEnd).toBe('function'))
    act(() => dnd.onDragEnd!({ active: { id: 'opus' }, over: { id: 'haiku' } }))
    await waitFor(() =>
      expect(patchConfigMock).toHaveBeenCalledWith('agent.model_order', ['opus', 'haiku', 'sonnet']),
    )
  })

  it('renders read-only with a hint when degraded, never overwriting the saved order', async () => {
    // Only Auto advertised (placeholder/degraded): the drag list must not render,
    // and no drop or reset can persist a truncated order over the saved one.
    modelsMock.mockReturnValue([{ name: 'auto', description: '' }])
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent: { model_order: ['opus', 'sonnet'] } }))
    wrap()
    expect(await screen.findByText('opus')).toBeInTheDocument()
    expect(screen.getByText('sonnet')).toBeInTheDocument()
    expect(screen.getByText(/reordering resumes once models load/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: RESET })).toBeNull()
    expect(screen.queryByLabelText(/^Reorder /)).toBeNull()
    expect(patchConfigMock).not.toHaveBeenCalled()
  })
})

describe('orderModelNames', () => {
  const m = (name: string) => ({ name })
  it('puts saved names first, skips stale ones, appends the rest in live order', () => {
    expect(orderModelNames([m('a'), m('b'), m('c')], ['c', 'a'])).toEqual(['c', 'a', 'b'])
    expect(orderModelNames([m('a'), m('b')], ['gone', 'b'])).toEqual(['b', 'a'])
    expect(orderModelNames([m('a'), m('b'), m('c')], [])).toEqual(['a', 'b', 'c'])
  })
  it('strips auto (the pinned row) wherever it appears', () => {
    expect(orderModelNames([m('auto'), m('a'), m('b')], ['b'])).toEqual(['b', 'a'])
    expect(orderModelNames([m('auto'), m('a')], ['auto', 'a'])).toEqual(['a'])
  })
})
