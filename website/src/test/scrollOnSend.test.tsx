/**
 * Settings -> Chat "Jump to the bottom when you send" (#25754).
 *
 * On by default, so a send still pins the transcript to the bottom. Off, a send
 * leaves the view alone. Following a turn that starts while the reader is at
 * the bottom is a different path and stays ungated.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { createRef } from 'react'

vi.mock('../chat-core/transport/sendTurn', async (orig) => ({
  ...(await orig<typeof import('../chat-core/transport/sendTurn')>()),
  // Never settles: the send-time scroll is scheduled before the receipt.
  sendTurn: vi.fn(() => new Promise(() => {})),
}))

import { store } from '../store'
import { useChatPaneSend } from '../components/useChatPaneSend'
import { loadChatConfig } from '../pages/chat/ChatSettings'
import type { ComposerHandle } from '../chat-core/composer/Composer'
import type { useChatPaneDraft } from '../components/useChatPaneDraft'
import composerSendSrc from '../pages/chat/page/composerSend.ts?raw'

const SLOT = 'chat-scroll-on-send'

function renderPaneSend(scrollToBottom: () => void) {
  const draft = {
    input: '', setInput: vi.fn(), pendingFiles: [], setPendingFiles: vi.fn(),
    pasteBlocks: [], setPasteBlocks: vi.fn(), readPastes: () => [],
    slotKeyRef: { current: SLOT }, inputRef: { current: '' }, mountedRef: { current: true },
    restoreIntoComposer: vi.fn(),
  } as unknown as ReturnType<typeof useChatPaneDraft>
  return renderHook(() => useChatPaneSend({
    slotKey: SLOT, dispatch: store.dispatch, draft, composerRef: createRef<ComposerHandle>(),
    busy: false, running: false, paneSlot: undefined, scrollToBottom, paneAgentName: 'default',
    pendingQuestion: null as never, markCrewmateSent: vi.fn(),
  }))
}

function sendAndSettle(scrollToBottom: () => void) {
  const { result } = renderPaneSend(scrollToBottom)
  act(() => { result.current.doSend('hello') })
  act(() => { vi.advanceTimersByTime(500) })
}

describe('scrollOnSend', () => {
  beforeEach(() => { localStorage.clear(); vi.useFakeTimers() })
  afterEach(() => { vi.useRealTimers(); localStorage.clear() })

  it('defaults on, and a non-boolean reads as on', () => {
    expect(loadChatConfig().scrollOnSend).toBe(true)
    localStorage.setItem('mc-chat-config', JSON.stringify({ scrollOnSend: 'no' }))
    expect(loadChatConfig().scrollOnSend).toBe(true)
  })

  it('on (default): a pane send scrolls to the bottom', () => {
    const scroll = vi.fn()
    sendAndSettle(scroll)
    expect(scroll).toHaveBeenCalledTimes(1)
  })

  it('off: a pane send leaves the view where it is', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ scrollOnSend: false }))
    const scroll = vi.fn()
    sendAndSettle(scroll)
    expect(scroll).not.toHaveBeenCalled()
  })

  it('the main chat gates only its send-time pin, not turn-start follow', () => {
    // The turn-start follow (autoFollowAllowed) keeps its own geometry gate and
    // must not read this setting; the send pin must.
    expect(composerSendSrc).toMatch(/if \(loadChatConfig\(\)\.scrollOnSend\) setTimeout\(\(\) => scrollBottom\(\), SCROLL_AFTER_RENDER_MS\)/)
    expect(composerSendSrc).toMatch(/autoFollowAllowed\(\)\) \{\n\s+setTimeout\(\(\) => scrollBottom\(\), SCROLL_AFTER_RENDER_MS\)/)
    expect(composerSendSrc.match(/scrollOnSend/g)).toHaveLength(1)
  })
})
