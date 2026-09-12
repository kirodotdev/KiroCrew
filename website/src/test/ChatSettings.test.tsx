/**
 * Covers the localStorage config helpers in pages/chat/ChatSettings.tsx.
 * The module's old default-exported settings popover was deleted with the
 * native <select> sweep — the live UI is ChatPanel/VoicePanel, tested there.
 */

import { describe, it, expect, beforeEach } from 'vitest'
import { loadChatConfig, DEFAULT_MESSAGE_FONT_SIZE, MIN_MESSAGE_FONT_SIZE, MAX_MESSAGE_FONT_SIZE } from '../pages/chat/ChatSettings'

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

  // Board view is opt-in: a client with no stored config must not inherit it,
  // or a gateway that already holds one tag column puts every fresh client
  // (new user, second browser, synced instance) into board view unasked.
  it('leaves board view off by default', () => {
    expect(loadChatConfig().tagColumnsEnabled).toBe(false)
  })

  it('respects a stored board-view opt-in', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ tagColumnsEnabled: true }))
    expect(loadChatConfig().tagColumnsEnabled).toBe(true)
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

  it('defaults messageFontSize to the pre-setting text-sm size', () => {
    expect(loadChatConfig().messageFontSize).toBe(DEFAULT_MESSAGE_FONT_SIZE)
  })

  it('respects a stored messageFontSize within bounds', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ messageFontSize: 18 }))
    expect(loadChatConfig().messageFontSize).toBe(18)
  })

  it('clamps a stored messageFontSize above the max', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ messageFontSize: MAX_MESSAGE_FONT_SIZE + 50 }))
    expect(loadChatConfig().messageFontSize).toBe(MAX_MESSAGE_FONT_SIZE)
  })

  it('clamps a stored messageFontSize below the min', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ messageFontSize: MIN_MESSAGE_FONT_SIZE - 50 }))
    expect(loadChatConfig().messageFontSize).toBe(MIN_MESSAGE_FONT_SIZE)
  })

  it('rounds a fractional stored messageFontSize', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ messageFontSize: 15.6 }))
    expect(loadChatConfig().messageFontSize).toBe(16)
  })

  it('repairs a non-number messageFontSize to the default', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ messageFontSize: 'huge' }))
    expect(loadChatConfig().messageFontSize).toBe(DEFAULT_MESSAGE_FONT_SIZE)
  })

  it('adopts the default messageFontSize for a stored config that predates the setting', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTimestamps: false }))
    const cfg = loadChatConfig()
    expect(cfg.messageFontSize).toBe(DEFAULT_MESSAGE_FONT_SIZE)
    expect(cfg.showTimestamps).toBe(false)
  })
})
