/** A chat an app keeps out of the session lists (`permissions.hiddenSessions`, mode
 *  = the app's id) has no sidebar row, so its tool prompt must still reach the
 *  person through the notices every chat's prompt raises: the chime and the OS toast. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import { QueryClient } from '@tanstack/react-query'
import { store } from '../../store'
import { sseSlots } from '../../store/dashboardSlice'
import { MC_NOTIFICATION_EVENT, APPROVAL_KIND } from '../notificationEvent'
import type { ChatSlot } from '../../types'
import { useChatStream } from './chatStream'
import type { StreamBuffers } from './streamBuffers'
import type { VoicePlayback } from './voicePlayback'
import { chatPageSlots } from '../../pages/chat/page/pageEnvironment'

const toasts = vi.hoisted(() => ({ posted: [] as { title: string; tag?: string }[] }))
vi.mock('../../lib/nativeNotify', async importOriginal => ({
  ...(await importOriginal<typeof import('../../lib/nativeNotify')>()),
  nativeNotificationPermitted: () => true,
  postNativeNotification: (title: string, opts?: { tag?: string }) => { toasts.posted.push({ title, tag: opts?.tag }) },
}))
vi.mock('../windowAway', () => ({ isWindowAway: () => true }))

const hidden = { key: 'acme-dj', title: 'Song finder', messages: 1, running: true, mode: 'acme-app' } as ChatSlot

describe("a hidden app chat's prompt still reaches the person", () => {
  const chimes: string[] = []
  const onChime = (e: Event) => { chimes.push((e as CustomEvent).detail?.kind) }

  beforeEach(() => {
    chimes.length = 0
    toasts.posted.length = 0
    store.dispatch(sseSlots([hidden]))
    window.addEventListener(MC_NOTIFICATION_EVENT, onChime)
  })
  afterEach(() => {
    window.removeEventListener(MC_NOTIFICATION_EVENT, onChime)
    store.dispatch(sseSlots([]))
  })

  it('has no row in the chat list, but stays listed for the app chat bound to it', () => {
    expect(chatPageSlots([hidden])).toEqual([])
    // An embedded `ChatPanel` passes the slot it is bound to, so the page keeps it active.
    expect(chatPageSlots([hidden], 'acme-dj')).toEqual([hidden])
    expect(chatPageSlots([hidden], 'other')).toEqual([])
  })

  it('its permission row chimes and posts an OS toast named for the chat', () => {
    const buffers = { flushChunks: vi.fn(), bufferSlotActivity: vi.fn() } as unknown as StreamBuffers
    const voice = { resetForNewTurn: vi.fn() } as unknown as VoicePlayback
    const { result } = renderHook(() => useChatStream({ dispatch: store.dispatch, queryClient: new QueryClient(), buffers, voice, reconnectingRef: { current: false } }))
    result.current.onChatMessage({ slot: 'acme-dj', role: 'permission', content: 'web_fetch', ts: '2026-11-14T19:00:00Z', meta: { approval_id: 'r1' } })
    expect(chimes).toEqual([APPROVAL_KIND])
    expect(toasts.posted).toEqual([{ title: 'Song finder', tag: 'kirocrew-approval:acme-dj:r1' }])
  })
})
