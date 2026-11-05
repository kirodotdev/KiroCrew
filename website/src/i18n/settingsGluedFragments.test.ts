import fs from 'node:fs'
import path from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './index'
import './all'
import { i18nT } from './t'

/**
 * Settings lines that were built from separate pieces: a translated word, then
 * a raw value or count, then another translated word. A language with another
 * word order, or a plural that agrees with the number, cannot be written that
 * way, so each line is one catalog string with the value passed in.
 */

const SRC = path.resolve(__dirname, '..')
const INST = 'pages.settings.instancesPanel'
const CHANNEL = 'pages.settings.channelDisabledPanel'
const POSTURE = 'pages.settings.postureDisclosure'
const BOT = 'pages.settings.botChannelPanel'

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('settings lines take their values inside the translated text', () => {
  it('reads as one English sentence each', () => {
    expect(i18nT(`${INST}.remote_port_ttl`, { port: 8000, ttl: '8h' })).toBe('· remote port 8000 · ttl 8h')
    expect(i18nT(`${INST}.warm_cap_count`, { count: 1 })).toMatch(/^Up to 1 remote crew stays warm/)
    expect(i18nT(`${INST}.warm_cap_count`, { count: 4 })).toMatch(/^Up to 4 remote crews stay warm/)
    expect(i18nT(`${CHANNEL}.channel_turned_off`, { channel: 'Slack' })).toBe('Slack is turned off by your administrator')
    expect(i18nT(`${CHANNEL}.channel_policy_status_unavailable`, { channel: 'Slack' })).toBe('Slack policy status unavailable')
    expect(i18nT(`${POSTURE}.show_more_count`, { count: 3 })).toBe('Show 3 more')
    expect(i18nT(`${INST}.remote_port_value`, { port: 8000 })).toBe('· remote port 8000')
    expect(i18nT(`${BOT}.loading_channel_config`, { channel: 'Slack' })).toBe('Loading Slack config…')
    expect(i18nT(`${BOT}.cannot_load_channel_config`, { channel: 'Slack' })).toBe('Cannot load Slack config. Is the gateway running?')
    expect(i18nT(`${BOT}.channel_settings_read_only`, { channel: 'Slack' })).toMatch(/^Slack settings are managed on the machine running /)
  })

  it('lets a language put the value where its grammar needs it', async () => {
    await i18next.changeLanguage('de')
    expect(i18nT(`${CHANNEL}.channel_policy_status_unavailable`, { channel: 'Slack' })).toBe('Richtlinienstatus für Slack nicht verfügbar')
    expect(i18nT(`${POSTURE}.show_more_count`, { count: 3 })).toBe('3 weitere anzeigen')
    expect(i18nT(`${BOT}.loading_channel_config`, { channel: 'Slack' })).toBe('Slack-Konfiguration wird geladen…')
    await i18next.changeLanguage('ja')
    expect(i18nT(`${POSTURE}.show_more_count`, { count: 3 })).toBe('さらに 3 件表示')
  })

  it('uses every Russian plural form for the warm-crew cap', async () => {
    await i18next.changeLanguage('ru')
    expect(i18nT(`${INST}.warm_cap_count`, { count: 1 })).toContain('не более 1 удалённой команды')
    expect(i18nT(`${INST}.warm_cap_count`, { count: 3 })).toContain('не более 3 удалённых команд')
    expect(i18nT(`${INST}.warm_cap_count`, { count: 5 })).toContain('не более 5 удалённых команд')
  })

  it.each([
    ['pages/settings/InstancesPanel.tsx', "{inst.remote_port} {i18nT("],
    ['pages/settings/InstancesPanel.tsx', "{warmCap} {i18nT("],
    ['pages/settings/ChannelDisabledPanel.tsx', "{label} {i18nT("],
    ['pages/settings/PostureDisclosure.tsx', "{hidden} {i18nT("],
    ['pages/settings/BotChannelPanel.tsx', "{spec.name} {i18nT("],
    ['pages/settings/BotChannelPanel.tsx', "${spec.name} ${i18nT("],
    ['pages/settings/RemoteCrewPanelCrewRow.tsx', "port_2')} ${inst.remote_port}"],
  ])('%s passes the value into the catalog (%s)', (file, glued) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(glued)
  })
})
