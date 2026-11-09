/**
 * Five Webhooks page lines as one translated message each.
 *
 * The confirm-hide button, the stored-context banner and a token's agent line
 * were each built from catalog pieces joined in code with an English space or
 * ": ". No language could order them its own way: Japanese, Korean and Chinese
 * read the confirm button as "confirm — hide permanently" in English word
 * order, and Japanese and Chinese got an ASCII colon or space between
 * full-width text. The stored-context source note and the test-request result
 * were split the same way around an inline `<code>` element, which each
 * message now carries as `<Trans>` markup.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const K = 'pages.webhooksPage.'
const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
const t = (lng: string, key: string, opts?: Record<string, string>) => i18next.getFixedT(lng)(K + key, opts) as string

describe('webhooks page whole messages', () => {
  it('keeps the English text the same', () => {
    expect(t('en', 'confirm_hide_permanently')).toBe('Confirm — hide permanently')
    expect(t('en', 'token_agent', { agent: 'reviewer' })).toBe('Agent: reviewer')
    expect(t('en', 'context_freshness_note', { effect: 'E.' })).toBe(
      'E. Context goes stale after 1 hour and expires after 24 hours.',
    )
    expect(t('en', 'context_written_by_tool')).toBe(
      "Written by the <tool/> tool. The agent reads it before the inbound message so the turn resumes with the prior session's intent.",
    )
    expect(t('en', 'test_session_started')).toBe("Session <session/> started. The agent's answer arrives in notifications.")
  })

  it('carries every placeholder in one message in every shipped language', () => {
    for (const lng of LANGS) {
      expect(t(lng, 'token_agent', { agent: 'zzz' })).toContain('zzz')
      expect(t(lng, 'context_freshness_note', { effect: 'QQQ' })).toContain('QQQ')
      expect(t(lng, 'confirm_hide_permanently')).not.toBe(t('en', 'confirm_hide_permanently'))
      expect(t(lng, 'context_written_by_tool').split('<tool/>')).toHaveLength(2)
      expect(t(lng, 'test_session_started').split('<session/>')).toHaveLength(2)
    }
  })

  it('puts "permanently" before "hide" in Japanese, Korean and Chinese', () => {
    expect(t('ja', 'confirm_hide_permanently')).toContain('恒久的に非表示')
    expect(t('ko', 'confirm_hide_permanently')).toContain('영구적으로 숨기기')
    expect(t('zh-CN', 'confirm_hide_permanently')).toContain('永久隐藏')
  })

  it('uses no ASCII colon or space between full-width text in Japanese and Chinese', () => {
    for (const lng of ['ja', 'zh-CN']) {
      expect(t(lng, 'token_agent', { agent: 'zzz' })).not.toContain(': ')
      expect(t(lng, 'context_freshness_note', { effect: '。' })).not.toContain('。 ')
    }
  })

  it('no longer glues these lines from parts', () => {
    const src = fs.readFileSync(path.join(SRC, 'pages/WebhooksPage.tsx'), 'utf8')
    expect(src).not.toContain("webhooksPage.permanently')")
    expect(src).not.toContain("pages.channelPage.agent')}:")
    expect(src).not.toContain("effect()} {i18nT(")
    expect(src).not.toContain("webhooksPage.written_by_the')")
    expect(src).not.toContain("webhooksPage.session')")
  })
})
