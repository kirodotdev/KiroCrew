/**
 * The Schedule page's "View last result" / "Go to Chat" (`useCronActions`
 * `openInChat`) keeps the cron tab it opens through a full `slots` frame that
 * omits it.
 *
 * `/to-chat` re-mints `cron-<id>` when the user has closed it and answers with
 * the key alone. Navigating to `/chat?slot=<key>` by itself only switches once
 * the key is listed, and the key is never stamped as a membership add, so a
 * full frame the gateway serialized before the re-mint (or one held in flight)
 * drops it after the switch. The chat page then clears the selection and
 * reopens the chat the user came from. On a fake-backend gateway that frame
 * bounced 10 of 10 trials before this hand-off went through `openCreatedSlot`.
 * Runs against the real reducers.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, waitFor } from '@testing-library/react'
import { useLocation } from 'react-router-dom'

import { createTestStore, renderHookWithProviders } from './helpers'
import { useCronActions } from '../hooks/useCronActions'
import { api } from '../api/client'
import { sseSlots, sseSlotPatch } from '../store/dashboardSlice'
import { switchSlot } from '../store/chatSlice'
import type { ChatSlot } from '../types'

vi.mock('../api/client', () => ({
  api: {
    cronToChat: vi.fn(),
    chatSlotDetail: vi.fn(),
  },
}))

const row = (key: string): ChatSlot => ({
  key, title: key, messages: 2, running: false, pending_approval: false, waiting_for_input: false, last_activity_ts: undefined,
})
const wire = (...keys: string[]): ChatSlot[] => keys.map(row)

beforeEach(() => {
  vi.mocked(api.cronToChat).mockReset().mockResolvedValue({ ok: true, slot: 'cron-job-1' })
  vi.mocked(api.chatSlotDetail).mockReset().mockResolvedValue({ messages: [], total: 0 })
})

describe('useCronActions openInChat with the cron tab closed and a stale slots frame', () => {
  it('opens the re-minted tab and keeps it through a frame that omits it', async () => {
    const store = createTestStore()
    // The run opened the job's tab, then the user closed it.
    store.dispatch(sseSlots(wire('origin', 'other', 'cron-job-1')))
    store.dispatch(sseSlotPatch({ slots: [], removed: ['cron-job-1'] }))
    await store.dispatch(switchSlot('origin'))
    const keys = () => store.getState().dashboard.slots.map(s => s.key)
    expect(keys()).not.toContain('cron-job-1')

    const { result } = renderHookWithProviders(
      () => ({ actions: useCronActions(() => {}), location: useLocation() }),
      { store, route: '/schedule' },
    )
    await act(async () => { await result.current.actions.openInChat('job-1') })

    expect(result.current.location.pathname).toBe('/chat')
    expect(result.current.location.search).toBe('?slot=cron-job-1')
    await waitFor(() => expect(store.getState().chat.activeSlot).toBe('cron-job-1'))
    expect(keys()).toContain('cron-job-1')

    // A full frame serialized before the re-mint lands after the switch.
    store.dispatch(sseSlots(wire('origin', 'other')))

    expect(keys()).toContain('cron-job-1')
    expect(store.getState().chat.activeSlot).toBe('cron-job-1')
    expect(result.current.actions.actionError).toBeNull()
  })
})
