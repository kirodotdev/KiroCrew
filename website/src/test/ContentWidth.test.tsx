import { describe, it, expect, vi, afterEach } from 'vitest'
import { CONTENT_WIDTH, loadChatConfig } from '../pages/chat/ChatSettings'

describe('Content Width', () => {
  afterEach(() => { vi.restoreAllMocks() })

  it('CONTENT_WIDTH map has correct values', () => {
    expect(CONTENT_WIDTH.compact.messages).toBe('800px')
    expect(CONTENT_WIDTH.compact.input).toBe('816px')
    expect(CONTENT_WIDTH.comfortable.messages).toBe('84%')
    expect(CONTENT_WIDTH.comfortable.input).toBe('85%')
    expect(CONTENT_WIDTH.full.messages).toBe('92%')
    expect(CONTENT_WIDTH.full.input).toBe('93%')
  })

  it('loadChatConfig falls back to compact on invalid contentWidth', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem')
      .mockReturnValue(JSON.stringify({ contentWidth: 'bogus' }))
    expect(loadChatConfig().contentWidth).toBe('compact')
    spy.mockRestore()
  })

  it('loadChatConfig preserves valid contentWidth', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem')
      .mockReturnValue(JSON.stringify({ contentWidth: 'full' }))
    expect(loadChatConfig().contentWidth).toBe('full')
    spy.mockRestore()
  })
})
