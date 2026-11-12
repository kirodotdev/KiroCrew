/**
 * Settings messages that were two translated parts joined in code.
 *
 * The update errors on the About panel glued a translated label to a
 * translated reason with an English `: `, and the Remote Crew sign-in note
 * glued two translated sentences with an English space. In Japanese and
 * Simplified Chinese that put a half-width colon or a stray space between
 * full-width text. Each is now one catalog message with the reason as a
 * placeholder, so every language words it the way it reads.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const A = 'pages.settings.aboutPanel.'
const R = 'pages.settings.remoteCrewPanel.'
const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
const t = (lng: string, key: string, opts?: Record<string, unknown>) =>
  i18next.getFixedT(lng)(key, opts) as string

describe('settings whole-message joins', () => {
  it('keeps the English update errors byte-identical', () => {
    expect(t('en', A + 'couldn_t_check_for_updates_reason', { reason: 'Offline.' }))
      .toBe("Couldn't check for updates: Offline.")
    expect(t('en', A + 'download_failed_reason', { reason: 'Offline.' })).toBe('Download failed: Offline.')
    expect(t('en', A + 'install_failed_reason', { reason: 'Offline.' })).toBe('Couldn’t install the update: Offline.')
  })

  it('uses a full-width colon with no space in Japanese and Simplified Chinese', () => {
    for (const lng of ['ja', 'zh-CN']) {
      for (const key of ['couldn_t_check_for_updates_reason', 'download_failed_reason', 'install_failed_reason']) {
        const v = t(lng, A + key, { reason: 'R' })
        expect(v.endsWith('：R')).toBe(true)
        expect(v).not.toContain(': ')
      }
    }
  })

  it('puts a narrow no-break space before the French colon', () => {
    expect(t('fr', A + 'download_failed_reason', { reason: 'R' }).endsWith('\u202f: R')).toBe(true)
  })

  it('carries the reason in every shipped language', () => {
    for (const lng of LANGS) {
      for (const key of ['couldn_t_check_for_updates_reason', 'download_failed_reason', 'install_failed_reason']) {
        const v = t(lng, A + key, { reason: 'zzz-reason' })
        expect(v).toContain('zzz-reason')
        expect(v).not.toBe(A + key)
      }
    }
  })

  it('words the restart sign-in note as one message with no stray CJK space', () => {
    expect(t('en', R + 'sign_in_unconfirmed_restart')).toBe(
      'We could not confirm the sign-in. The code below may still work — finish it here, then connect the crew. If it has expired, start over with a new code.',
    )
    for (const lng of ['ja', 'zh-CN']) {
      const v = t(lng, R + 'sign_in_unconfirmed_restart')
      expect(v).not.toContain('。 ')
      expect(v.startsWith(t(lng, R + 'sign_in_unconfirmed'))).toBe(true)
    }
    for (const lng of LANGS) expect(t(lng, R + 'sign_in_unconfirmed_restart')).not.toBe(R + 'sign_in_unconfirmed_restart')
  })

  it.each([
    ['pages/settings/AboutPanel.tsx', "aboutPanel.couldn_t_check_for_updates')}: "],
    ['pages/settings/AboutPanel.tsx', "aboutPanel.download_failed')}: "],
    ['pages/settings/AboutPanel.tsx', "aboutPanel.install_failed')}: "],
    ['pages/settings/AboutPanel.tsx', "aboutPanel.download_failed')}: ${updateErrorText"],
    ['pages/settings/RemoteCrewPanelLaunch.tsx', "sign_in_unconfirmed')} ${i18nT("],
  ])('%s no longer glues translated parts (%s)', (file, literal) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(literal)
  })
})
