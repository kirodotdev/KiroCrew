import { describe, it, expect } from 'vitest'
import { slotChannelLabel, slotChannelNamespace } from './channelOrigin'

describe('slotChannelLabel', () => {
  it('labels every channel namespace', () => {
    expect(slotChannelLabel('slack:1785370133.085469')).toBe('Slack')
    expect(slotChannelLabel('discord:kirocrew:direct:U1')).toBe('Discord')
    expect(slotChannelLabel('telegram:kirocrew:direct:U1')).toBe('Telegram')
    expect(slotChannelLabel('whatsapp:kirocrew:direct:U1')).toBe('WhatsApp')
    expect(slotChannelLabel('webex:kirocrew:direct:U1')).toBe('Webex')
    expect(slotChannelLabel('wecom:kirocrew:direct:U1')).toBe('WeCom')
    expect(slotChannelLabel('teams:kirocrew:direct:U1')).toBe('Teams')
    expect(slotChannelLabel('weixin:kirocrew:direct:U1')).toBe('Weixin')
    expect(slotChannelLabel('unified:kirocrew')).toBe('Direct message')
  })

  it('labels the persisted filename-stem form too', () => {
    // list_sessions() reports the stem, where history._safe_key folded ':' -> '_'.
    expect(slotChannelLabel('slack_1785370133.085469')).toBe('Slack')
    expect(slotChannelLabel('discord_kirocrew_direct_U1')).toBe('Discord')
    expect(slotChannelLabel('unified_kirocrew')).toBe('Direct message')
  })

  it('returns empty for non-channel producers so no glyph renders', () => {
    expect(slotChannelLabel('cron:abc123')).toBe('')
    expect(slotChannelLabel('cron_abc123')).toBe('')
    expect(slotChannelLabel('hook:default:1')).toBe('')
    expect(slotChannelLabel('subagent:xyz')).toBe('')
    expect(slotChannelLabel('dashboard:chat-1-1')).toBe('')
    expect(slotChannelLabel('dashboard_chat-1-1')).toBe('')
    expect(slotChannelLabel('channel:general')).toBe('')
  })

  it('returns empty for an unlinked slot', () => {
    expect(slotChannelLabel(undefined)).toBe('')
    expect(slotChannelLabel('')).toBe('')
  })

  it('does not match a namespace by prefix alone', () => {
    expect(slotChannelLabel('slackish:1.2')).toBe('')
  })

  it('is case-sensitive so a user-titled session is not mislabelled', () => {
    // A title-derived slot name like "Slack thread triage" folds to
    // "Slack_thread_triage" — capital S, so it is NOT channel-origin.
    expect(slotChannelLabel('Slack_thread_triage')).toBe('')
    expect(slotChannelLabel('Teams_sync_notes')).toBe('')
  })
})

describe('slotChannelNamespace', () => {
  it('returns the namespace for both key separators', () => {
    expect(slotChannelNamespace('slack:1785370133.085469')).toBe('slack')
    expect(slotChannelNamespace('slack_1785370133.085469')).toBe('slack')
    expect(slotChannelNamespace('discord:bot:direct:u1')).toBe('discord')
  })

  it('singles out unified, which has no proper-noun label to interpolate', () => {
    // The tooltip routes this namespace to its own locale key so a translated
    // sentence never has to embed the English article form.
    expect(slotChannelNamespace('unified:kirocrew')).toBe('unified')
    expect(slotChannelNamespace('unified_kirocrew')).toBe('unified')
  })

  it('returns empty for dashboard sessions and non-channel keys', () => {
    expect(slotChannelNamespace('dashboard:chat-1-1')).toBe('')
    expect(slotChannelNamespace('slackish:1.2')).toBe('')
    expect(slotChannelNamespace('Slack_thread_triage')).toBe('')
    expect(slotChannelNamespace(undefined)).toBe('')
    expect(slotChannelNamespace('')).toBe('')
  })

  it('agrees with slotChannelLabel on what counts as channel-origin', () => {
    for (const key of ['slack:1.1', 'unified_kirocrew', 'dashboard:chat-1', 'Slack_x', '']) {
      expect(Boolean(slotChannelNamespace(key))).toBe(Boolean(slotChannelLabel(key)))
    }
  })
})
