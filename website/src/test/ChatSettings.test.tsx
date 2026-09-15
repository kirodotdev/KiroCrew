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

  it('leaves an empty folder its body until the user opts in', () => {
    // OFF is the whole contract of this setting: it changes how every empty folder
    // in the sidebar reads and removes their only labelled create row, so a client
    // with no stored config must never inherit it.
    expect(loadChatConfig().hideEmptyFolderBody).toBe(false)
    localStorage.setItem('mc-chat-config', JSON.stringify({ showTimestamps: false }))
    expect(loadChatConfig().hideEmptyFolderBody).toBe(false)
  })

  it('respects stored hideEmptyFolderBody=true', () => {
    localStorage.setItem('mc-chat-config', JSON.stringify({ hideEmptyFolderBody: true }))
    expect(loadChatConfig().hideEmptyFolderBody).toBe(true)
  })

  it('repairs a non-boolean hideEmptyFolderBody value to the disabled default', () => {
    // A truthy string must not switch the feature on: the sidebar reads this flag
    // directly, so an unrepaired value would opt a user in by accident.
    localStorage.setItem('mc-chat-config', JSON.stringify({ hideEmptyFolderBody: 'yes' }))
    expect(loadChatConfig().hideEmptyFolderBody).toBe(false)
  })
})
