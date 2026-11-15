/**
 * Separately translated sentences shown as one message are joined the way the
 * language joins sentences (#36768): a space in English, nothing after a `。`
 * in Japanese and Chinese.
 */

import { afterEach, describe, expect, it } from 'vitest'

import { i18next } from './all'
import { joinSentences } from './format'
import { i18nT } from './t'
import { appActionNotice } from '../components/appstore/backendRefused'
import { registryRefreshFailureMessage } from '../components/appstore/registryRefreshMessage'
import { settingHelpText } from '../components/commandPalette/settingsTypes'
import ja from './locales/ja.json'
import zhCN from './locales/zh-CN.json'

afterEach(async () => {
  await i18next.changeLanguage('en')
})

describe('joinSentences', () => {
  it('puts one space between sentences in English and Korean', async () => {
    expect(joinSentences(['One.', 'Two.'])).toBe('One. Two.')
    await i18next.changeLanguage('ko')
    expect(joinSentences(['하나.', '둘.'])).toBe('하나. 둘.')
  })

  it('puts no space between sentences in Japanese and Chinese', async () => {
    await i18next.changeLanguage('ja')
    expect(joinSentences(['一つ。', '二つ。'])).toBe('一つ。二つ。')
    await i18next.changeLanguage('zh-CN')
    expect(joinSentences(['一。', '二。'])).toBe('一。二。')
  })

  it('drops empty entries so no separator is left behind', () => {
    expect(joinSentences(['', 'One.', undefined, false, null, 'Two.'])).toBe('One. Two.')
    expect(joinSentences(['', undefined])).toBe('')
  })
})

describe('messages built from two translated sentences', () => {
  it('leave no space after the 。 of the app enable notice in Japanese', async () => {
    await i18next.changeLanguage('ja')
    const notice = appActionNotice('Notes', { backend_refused: 'no exec。', warnings: ['hooks failed'] }, 'enable')
    expect(notice).toBe(
      i18nT('pages.appsPage.backend_refused_notice', { name: 'Notes', reason: 'no exec。' })
      + i18nT('pages.appsPage.action_warnings_enabled', { name: 'Notes', warnings: 'hooks failed' }),
    )
  })

  it('keep the space when the first sentence ends in a raw English reason', async () => {
    await i18next.changeLanguage('ja')
    expect(joinSentences(['理由: no exec', '二つ。'])).toBe('理由: no exec 二つ。')
  })

  it('leave no space between the registry sentences in Chinese', async () => {
    await i18next.changeLanguage('zh-CN')
    const message = registryRefreshFailureMessage({
      ok: false,
      failed: ['a', 'b'],
      results: [{ name: 'a', ok: false, cached: false }, { name: 'b', ok: false }],
    })
    expect(message).toBe(
      i18nT('components.registryManager.registry_apps_missing', { names: 'a' })
      + i18nT('components.registryManager.could_not_refresh_still_showing_last_synced', { names: 'b' }),
    )
  })

  it('run a setting tip into its description with no space in Japanese', async () => {
    await i18next.changeLanguage('ja')
    expect(settingHelpText({ hint: '一つ。', description: '二つ。' })).toBe('一つ。二つ。')
    await i18next.changeLanguage('en')
    expect(settingHelpText({ hint: 'One.', description: 'Two.' })).toBe('One. Two.')
  })
})

describe('the Atlassian card tip', () => {
  it('has no space after its first 。 in Japanese and Chinese', () => {
    expect(ja.pages.connectionsPage.prerequisite_atlassian).not.toContain('。 ')
    expect(zhCN.pages.connectionsPage.prerequisite_atlassian).not.toContain('。 ')
  })
})
