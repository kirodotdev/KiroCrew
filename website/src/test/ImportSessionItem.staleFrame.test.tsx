/**
 * An imported session survives a full `slots` frame the gateway built before
 * the import.
 *
 * A full frame carries no ordering mark, so one serialized before the import
 * POST can reach the tab after the row has opened the imported chat. Applied as
 * a whole-list replace it drops the new key, and the chat page then clears the
 * active session and opens another one. The dashboard reducer keeps a key
 * through such a frame only when the add was stamped as a membership add, so
 * this runs the row against the REAL dashboard reducer: a mocked slice would
 * hide whether the import reaches that stamp.
 */
import * as React from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { configureStore } from '@reduxjs/toolkit'
import type { ChatSlot } from '../types'

const mocks = vi.hoisted(() => ({ importSessionFromFile: vi.fn(), chatSlots: vi.fn() }))
vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue([])),
  }),
}))

const holder = vi.hoisted(() => ({
  dispatch: null as null | ((a: unknown) => unknown),
  listed: null as null | (() => string[]),
}))
vi.mock('../store', () => ({
  useAppDispatch: () => (a: unknown) => holder.dispatch!(a),
}))
const switched = vi.hoisted(() => [] as unknown[])
// The slot keys the list held at the moment each switch was built.
const listedAtSwitch = vi.hoisted(() => [] as string[][])
vi.mock('../store/chatSlice', () => ({
  switchSlot: (arg: unknown) => {
    switched.push(arg)
    listedAtSwitch.push(holder.listed?.() ?? [])
    return { type: 'test/switchSlot' }
  },
}))

import dashboard, { sseSlots } from '../store/dashboardSlice'
import ImportSessionItem from '../components/ImportSessionItem'

const row = (key: string): ChatSlot => ({
  key, title: key, messages: 2, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})
const wire = (...keys: string[]): ChatSlot[] => keys.map(row)

function StubItem({ disabled, onSelect, children }: {
  disabled?: boolean
  onSelect?: (event: Event) => void
  children?: React.ReactNode
}) {
  return (
    <button type="button" disabled={disabled} data-testid="row" onClick={() => onSelect?.(new Event('select'))}>
      {children}
    </button>
  )
}

function makeStore() {
  const store = configureStore({ reducer: { dashboard } })
  holder.dispatch = store.dispatch as (a: unknown) => unknown
  holder.listed = () => store.getState().dashboard.slots.map(s => s.key)
  store.dispatch(sseSlots(wire('origin', 'other')))
  return store
}

async function importFile() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
      <ImportSessionItem Item={StubItem} />
    </QueryClientProvider>,
  )
  await userEvent.click(screen.getByTestId('row'))
  const input = document.body.querySelector('input[type="file"]') as HTMLInputElement
  await userEvent.upload(input, new File([new Uint8Array([0x1f, 0x8b])], 'chat.kcsession.json.gz', { type: 'application/gzip' }))
  await waitFor(() => expect(switched).toEqual([{ key: 'imported-1', keepTargetOnMissing: true }]))
}

const keys = (store: ReturnType<typeof makeStore>) => store.getState().dashboard.slots.map(s => s.key)

describe('ImportSessionItem and a stale slots frame', () => {
  beforeEach(() => {
    switched.length = 0
    listedAtSwitch.length = 0
    document.body.querySelector('input[type="file"]')?.remove()
    mocks.importSessionFromFile.mockReset().mockResolvedValue({
      ok: true, key: 'imported-1', title: 'chat (from mac)', messages: 3, resume_mode: 'prefix',
    })
    mocks.chatSlots.mockReset().mockResolvedValue(wire('origin', 'other', 'imported-1'))
  })

  it('keeps the imported session through a frame built before the import', async () => {
    const store = makeStore()
    await importFile()
    expect(keys(store)).toContain('imported-1')

    store.dispatch(sseSlots(wire('origin', 'other')))

    expect(keys(store)).toContain('imported-1')
  })

  it('drops the imported session when a list read sent after the import omits it', async () => {
    // Deleted in another tab right after the import: the reducer's settle rule
    // still applies, so the row is not kept for the whole retention window.
    mocks.chatSlots.mockReset().mockResolvedValue(wire('origin', 'other'))
    const store = makeStore()
    await importFile()
    store.dispatch(sseSlots(wire('origin', 'other')))
    expect(keys(store)).not.toContain('imported-1')
  })

  it('switches to a listed key when the list refresh fails', async () => {
    // Before the membership add, a rejected refresh left the switch pointing at
    // a key the slot list lacked, and the chat page clears such a selection.
    mocks.chatSlots.mockReset().mockRejectedValue(new Error('gateway unavailable'))
    const store = makeStore()
    await importFile()
    expect(listedAtSwitch).toHaveLength(1)
    expect(listedAtSwitch[0]).toContain('imported-1')
    expect(keys(store)).toContain('imported-1')
  })

  it('lists the imported session while a slow list refresh is in flight', async () => {
    // The switch still waits for the refresh. While it is in flight the row
    // is already listed, so a stale frame landing in that window cannot drop
    // the imported key.
    let answer: (rows: ChatSlot[]) => void = () => {}
    mocks.chatSlots.mockReset().mockReturnValue(new Promise<ChatSlot[]>(r => { answer = r }))
    const store = makeStore()
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
        <ImportSessionItem Item={StubItem} />
      </QueryClientProvider>,
    )
    await userEvent.click(screen.getByTestId('row'))
    const input = document.body.querySelector('input[type="file"]') as HTMLInputElement
    await userEvent.upload(input, new File([new Uint8Array([0x1f, 0x8b])], 'a.json.gz', { type: 'application/gzip' }))
    await waitFor(() => expect(mocks.chatSlots).toHaveBeenCalled())
    expect(keys(store)).toContain('imported-1')
    expect(store.getState().dashboard.slots.find(s => s.key === 'imported-1')?.title).toBe('chat (from mac)')
    answer(wire('origin', 'other', 'imported-1'))
    await waitFor(() => expect(switched).toHaveLength(1))
    await waitFor(() => expect(screen.getByTestId('row')).not.toBeDisabled())
  })
})
