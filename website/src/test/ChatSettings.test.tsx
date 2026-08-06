/**
 * Covers the localStorage config helpers in pages/chat/ChatSettings.tsx.
 * The module's old default-exported settings popover was deleted with the
 * native <select> sweep — the live UI is ChatPanel/VoicePanel, tested there.
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { loadChatConfig } from '../pages/chat/ChatSettings'

describe('loadChatConfig', () => {
  beforeEach(() => { localStorage.removeItem('mc-chat-config') })

  it('defaults sendOnEnter to true', () => {
    expect(loadChatConfig().sendOnEnter).toBe('enter')
  })

  it('respects stored sendOnEnter=false', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ sendOnEnter: false }))
    expect(loadChatConfig().sendOnEnter).toBe('ctrl-enter')
  })

  it('defaults confirmCloseSession to false', () => {
    expect(loadChatConfig().confirmCloseSession).toBe(false)
  })

  it('respects stored confirmCloseSession=true', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ confirmCloseSession: true }))
    expect(loadChatConfig().confirmCloseSession).toBe(true)
  })

  it('shows turn stats by default', () => {
    expect(loadChatConfig().showTurnStats).toBe(true)
  })

  it('respects stored showTurnStats=false', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTurnStats: false }))
    expect(loadChatConfig().showTurnStats).toBe(false)
  })

  it('repairs a non-boolean showTurnStats value to the enabled default', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTurnStats: 'no' }))
    expect(loadChatConfig().showTurnStats).toBe(true)
  })

  it('pins the latest prompt by default', () => {
    expect(loadChatConfig().pinLastPrompt).toBe(true)
  })

  it('adopts the new default for a stored config that predates the setting', () => {
    // Configs written before the sticky-banner setting existed carry no
    // pinLastPrompt key, so the DEFAULTS spread is what upgrades them.
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTimestamps: false }))
    const cfg = loadChatConfig()
    expect(cfg.pinLastPrompt).toBe(true)
    expect(cfg.showTimestamps).toBe(false)
  })

  it('respects stored pinLastPrompt=false', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false }))
    expect(loadChatConfig().pinLastPrompt).toBe(false)
  })

  it('repairs a non-boolean pinLastPrompt value to the enabled default', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: 'yes' }))
    expect(loadChatConfig().pinLastPrompt).toBe(true)
  })
})
