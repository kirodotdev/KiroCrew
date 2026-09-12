import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import AutoNudgePopover, { type AutoNudgeLoop } from '../components/AutoNudgePopover'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('clears a reduced retained monitor row without a message', async () => {
  // The legacy slot read withholds the structured monitor message and banner.
  const reduced: AutoNudgeLoop = JSON.parse(JSON.stringify({
    id: 'retained-monitor', slot_key: 'chat-1', active: false,
    idle_secs: 300, max_cycles: 8, cycle_count: 2,
    last_fire_ts: 0, next_due_ts: 0, stopped_reason: 'manual',
  }))
  const calls: string[] = []
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === 'DELETE') calls.push(String(url))
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) })
  }))
  const onChange = vi.fn()
  const onOpenChange = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  render(
    <QueryClientProvider client={client}>
      <AutoNudgePopover slotKey="chat-1" loop={reduced} open={true}
        onChange={onChange} onOpenChange={onOpenChange} />
    </QueryClientProvider>,
  )
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear stopped goal' })) })
  expect(calls).toEqual([])
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Clear goal for good' })) })
  expect(calls).toEqual(['/api/autonudge/retained-monitor?intent=clear'])
  expect(onChange).toHaveBeenCalledWith(null)
  expect(onOpenChange).toHaveBeenCalledWith(false)
})
