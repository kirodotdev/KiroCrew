/**
 * Four messages that were two translated sentences joined in code.
 *
 * The source-links stop note, the browser permission explainer, the usage
 * chart's slider label and the crew memory-binding notice each glued two
 * catalog messages with an English space (or `. `). In Japanese and
 * Simplified Chinese that put an ASCII space, or an ASCII full stop, between
 * full-width sentences. Each is now one catalog message.
 */
import fs from 'node:fs'
import path from 'node:path'
import { describe, expect, it } from 'vitest'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../i18n/all'

const SRC = path.resolve(__dirname, '..')
const LANGS = ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']
const t = (lng: string, key: string, opts?: Record<string, unknown>) =>
  i18next.getFixedT(lng)(key, opts) as string

const KEYS: Array<[string, Record<string, unknown>]> = [
  ['pages.chatSidebar.hide_all_source_links_stopped_retry', { done: 2, n: 5 }],
  ['pages.chat.browserLiveCard.allow_explainer_asked_once', { productName: 'Kiro Crew' }],
  ['pages.overview.usageSeriesChart.aria_label_keyboard_hint', {}],
  ['pages.kiroCrewAgentsPage.memory_binding_mismatch_diagnostic', { command: 'kirocrew doctor' }],
  ['pages.kiroCrewAgentsPage.memory_binding_unavailable_diagnostic', { command: 'kirocrew doctor' }],
]

describe('whole-message joins outside Settings', () => {
  it('keeps the English text the same', () => {
    expect(t('en', KEYS[0][0], KEYS[0][1])).toBe(
      'Stopped after hiding 2 of 5 chips. The chips still listed were not hidden. To try again, hide the rest from this menu.',
    )
    expect(t('en', KEYS[2][0])).toBe(
      'Stacked area chart of spend over time. Use the left and right arrow keys to step through days.',
    )
    expect(t('en', KEYS[3][0], KEYS[3][1])).toBe(
      'This member’s configured memory store belongs to another member. It cannot be used here. Inspect the cause on the gateway: kirocrew doctor',
    )
    expect(t('en', KEYS[4][0], KEYS[4][1])).toBe(
      'This member’s configured memory store is unavailable. Inspect the cause on the gateway: kirocrew doctor',
    )
  })

  it('puts no ASCII space or full stop between Japanese and Chinese sentences', () => {
    for (const lng of ['ja', 'zh-CN']) {
      for (const [key, opts] of KEYS) {
        const v = t(lng, key, opts)
        expect(v).not.toContain('。 ')
        expect(v).not.toContain('. ')
      }
      expect(t(lng, KEYS[2][0])).toContain('。')
    }
  })

  it('has every message in each shipped language', () => {
    for (const lng of LANGS) {
      for (const [key, opts] of KEYS) {
        const v = t(lng, key, opts)
        expect(v).not.toBe(key)
        expect(v).not.toBe(t('en', key, opts))
      }
    }
  })

  it.each([
    ['components/SourceLinksSubmenu.tsx', "hide_all_source_links_stopped', { done: walkStopped.done, n: walkStopped.total })} ${i18nT("],
    ['pages/chat/BrowserLiveCardViews.tsx', "allow_explainer')} {i18nT("],
    ['pages/overview/TokenStackedAreaChart.tsx', "usageSeriesChart.aria_label')}. ${i18nT("],
    ['components/crew/CrewEditorNextDialog.tsx', "memory_binding_unavailable')} ${i18nT("],
  ])('%s no longer glues translated sentences (%s)', (file, literal) => {
    expect(fs.readFileSync(path.join(SRC, file), 'utf8')).not.toContain(literal)
  })
})
