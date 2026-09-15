import { afterEach, describe, expect, it, vi } from 'vitest'

import { mintSendId } from '../pages/chat/ChatPageMessageContent'

describe('ChatPage optimistic send correlation ids', () => {
  afterEach(() => vi.restoreAllMocks())

  it('preserves the legacy timestamp-and-base36-nonce wire format', () => {
    vi.spyOn(Date, 'now').mockReturnValue(36)
    vi.spyOn(Math, 'random').mockReturnValue(0.5)

    expect(mintSendId()).toBe('s-10-i')
  })
})
