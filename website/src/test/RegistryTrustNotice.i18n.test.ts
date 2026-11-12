/**
 * The registry trust banner as one translated message.
 *
 * The banner was five catalog pieces joined in code with English spaces
 * ("You are now trusting apps from" + hosts + ". Apps from" + host noun +
 * the rest). No language could order the sentence its own way, and in
 * Japanese and Simplified Chinese the joins put ASCII spaces between
 * full-width text. It is now one message per host count, with the host
 * list as a placeholder.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const K = 'components.registryManager.'
const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
const t = (lng: string, key: string, hosts: string) => i18next.getFixedT(lng)(K + key, { hosts }) as string

describe('registry trust banner', () => {
  it('keeps the English text the same', () => {
    expect(t('en', 'trust_notice_single', 'github.com')).toBe(
      'You are now trusting apps from github.com. Apps from this host become installable and run setup with gateway privileges.',
    )
    expect(t('en', 'trust_notice_multiple', 'a.dev, b.dev')).toBe(
      'You are now trusting apps from a.dev, b.dev. Apps from these hosts become installable and run setup with gateway privileges.',
    )
  })

  it('puts the host list inside one message in every shipped language', () => {
    for (const lng of LANGS) {
      for (const key of ['trust_notice_single', 'trust_notice_multiple']) {
        const v = t(lng, key, 'zzz.example')
        expect(v).toContain('zzz.example')
        expect(v).not.toBe(t('en', key, 'zzz.example'))
      }
    }
  })

  it('puts no ASCII space after a full-width stop in Japanese and Chinese', () => {
    for (const lng of ['ja', 'zh-CN']) {
      for (const key of ['trust_notice_single', 'trust_notice_multiple']) {
        expect(t(lng, key, 'zzz.example')).not.toContain('。 ')
        expect(t(lng, key, 'zzz.example')).not.toContain('. ')
      }
    }
  })

  it('no longer glues the banner from parts', () => {
    const src = fs.readFileSync(path.join(SRC, 'components/RegistryManager.tsx'), 'utf8')
    expect(src).not.toContain("registryManager.apps_from')")
    expect(src).not.toContain("registryManager.you_are_now_trusting_apps_from')")
  })
})
