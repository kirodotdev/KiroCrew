import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { loadChatConfig, saveChatConfig } from '../pages/chat/ChatSettings'
import { useComposerInlineMarkdown } from '../hooks/useComposerInlineMarkdown'

describe('composer inline markdown preference', () => {
  afterEach(() => localStorage.clear())

  it('defaults to off for a client with no stored config', () => {
    localStorage.clear()
    expect(loadChatConfig().inlineMarkdown).toBe(false)
  })

  it('falls back to off when the stored value is not a boolean', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ inlineMarkdown: 'yes' }))
    expect(loadChatConfig().inlineMarkdown).toBe(false)
  })

  it('follows the saved setting live', () => {
    const { result } = renderHook(() => useComposerInlineMarkdown())
    expect(result.current).toBe(false)
    act(() => {
      saveChatConfig({ ...loadChatConfig(), inlineMarkdown: true })
      window.dispatchEvent(new Event('mc-config-changed'))
    })
    expect(result.current).toBe(true)
  })
})
