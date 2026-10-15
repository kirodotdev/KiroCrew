// @vitest-environment jsdom
/**
 * Every built-in dashboard template speaks the reader's UI language.
 *
 * The gateway puts the reader's checked locale on `window.kirocrew.locale`. Each
 * template carries its own small string table per language and picks by it, with
 * English for any key a language lacks and for any locale it has no table for.
 * What is checked here is the page's real script in a real DOM, the way
 * `projectReportPage.test.ts` drives one template: every built-in, with an empty
 * field bag, in zh-CN, in English, and in a locale no template has a table for.
 *
 * zh-CN strings are written as `\u` escapes so this file stays ASCII.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BUILTIN = resolve(__dirname, '../../../src/kiro_crew/dashboard_templates/builtin')
const IDS = readdirSync(BUILTIN, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => d.name)
  .sort()

const CJK = /[\u3000-\u9fff\uff00-\uffef]/

/**
 * The app's zh-CN catalog calls a crewmate \u961f\u53cb and credits \u989d\u5ea6. A
 * template that says it another way puts two words for one thing on one screen.
 */
const OFF_CATALOG: Record<string, string> = {
  '\u6210\u5458': 'crewmate', '\u540c\u4e8b': 'crewmate', '\u7ec4\u5458': 'crewmate',
  '\u70b9\u6570': 'credits', '\u79ef\u5206': 'credits', '\u4fe1\u7528': 'credits',
}

/** The page's source with every \u escape decoded, so its zh-CN table can be read. */
function decoded(id: string) {
  return source(id).replace(/\\u([0-9a-fA-F]{4})/g, (_, hex: string) => String.fromCharCode(parseInt(hex, 16)))
}

/**
 * One page's own words in both languages, as an empty field bag renders them.
 * A string the page shows before any value arrives, so a template that ignored
 * the locale shows the English one under zh-CN and fails here.
 */
const PROBES: Record<string, { en: string; zh: string }> = {
  'kirocrew-conductor': { en: 'No tasks on this board yet.', zh: '\u770b\u677f\u4e0a\u8fd8\u6ca1\u6709\u4efb\u52a1\u3002' },
  'goal-board': { en: 'No item on this board yet.', zh: '\u770b\u677f\u4e0a\u8fd8\u6ca1\u6709\u4efb\u4f55\u4e8b\u9879\u3002' },
  'project-report': {
    en: 'No workstream yet. Nothing needs you, and nothing has been spent.',
    zh: '\u6682\u65e0\u5de5\u4f5c\u6d41\u3002\u6ca1\u6709\u5f85\u4f60\u5904\u7406\u7684\u4e8b\uff0c\u4e5f\u6ca1\u6709\u4efb\u4f55\u82b1\u8d39\u3002',
  },
  office: {
    en: 'The record carries no board for this mate yet, so there is nothing to review.',
    zh: '\u8fd9\u4f4d\u961f\u53cb\u7684\u8bb0\u5f55\u4e2d\u8fd8\u6ca1\u6709\u770b\u677f\uff0c\u56e0\u6b64\u6ca1\u6709\u53ef\u5ba1\u6838\u7684\u5185\u5bb9\u3002',
  },
  'org-chart': {
    en: 'No board has reached this mate\u2019s record yet, so there is nobody to chart.',
    zh: '\u5c1a\u65e0\u770b\u677f\u8fdb\u5165\u8fd9\u4f4d\u961f\u53cb\u7684\u8bb0\u5f55\uff0c\u56e0\u6b64\u6ca1\u6709\u53ef\u7ed8\u5236\u7684\u5bf9\u8c61\u3002',
  },
  'work-kanban': { en: 'This board carries no item yet.', zh: '\u6b64\u770b\u677f\u8fd8\u6ca1\u6709\u4efb\u4f55\u4e8b\u9879\u3002' },
  timeline: {
    en: 'No board has reached this mate\u2019s record yet.',
    zh: '\u8fd8\u6ca1\u6709\u770b\u677f\u8fdb\u5165\u8fd9\u4f4d\u961f\u53cb\u7684\u8bb0\u5f55\u3002',
  },
  'pr-watch': {
    en: 'No lane reading yet. The mate has not written one.',
    zh: '\u8fd8\u6ca1\u6709\u68c0\u67e5\u8bfb\u6570\u3002\u961f\u53cb\u5c1a\u672a\u5199\u5165\u3002',
  },
  flow: { en: 'No item on this board yet.', zh: '\u770b\u677f\u4e0a\u8fd8\u6ca1\u6709\u4efb\u4f55\u4e8b\u9879\u3002' },
  standup: { en: 'no headline written', zh: '\u672a\u5199\u5165\u8981\u70b9' },
  swimlane: {
    en: 'No item on this board yet, so there is no lane to draw.',
    zh: '\u770b\u677f\u4e0a\u8fd8\u6ca1\u6709\u4efb\u4f55\u4e8b\u9879\uff0c\u56e0\u6b64\u6ca1\u6709\u53ef\u7ed8\u5236\u7684\u6cf3\u9053\u3002',
  },
  roadmap: { en: 'No item on this board yet.', zh: '\u770b\u677f\u4e0a\u8fd8\u6ca1\u6709\u4efb\u4f55\u4e8b\u9879\u3002' },
  'session-ledger': { en: 'Nothing rejected yet.', zh: '\u5c1a\u65e0\u5426\u51b3\u9879\u3002' },
}

/** The page's raw source. */
function source(id: string) {
  return readFileSync(resolve(BUILTIN, id, 'template.html'), 'utf-8')
}

/**
 * The page in two halves, split by the DOM rather than by a pattern: the parsed
 * scripts' text, and the parsed body with every script element removed.
 */
function halves(id: string) {
  const doc = new DOMParser().parseFromString(source(id), 'text/html')
  const nodes = Array.from(doc.querySelectorAll('script'))
  const scripts = nodes.map((node) => node.textContent ?? '')
  for (const node of nodes) node.remove()
  return { scripts, body: doc.body }
}

function mount(id: string, locale: string | undefined) {
  const { scripts, body } = halves(id)
  ;(window as unknown as { kirocrew: unknown }).kirocrew = {
    fields: {},
    agentic: [],
    seq: 1,
    stale: false,
    missing: [],
    written_at: {},
    ...(locale === undefined ? {} : { locale }),
  }
  document.body.replaceChildren(...Array.from(body.childNodes))
  for (const s of scripts) new Function(s)()
  return document.body.textContent ?? ''
}

describe('built-in dashboard templates pick their words by the reader locale', () => {
  it('finds all thirteen built-ins, so the cases below are not over an empty list', () => {
    expect(IDS).toHaveLength(13)
  })

  it('labels the project-report All pill in the reader language', () => {
    mount('project-report', 'zh-CN')
    const pills = Array.from(document.querySelectorAll('button, span')).map((n) => (n.textContent ?? '').trim())
    expect(pills.some((t) => /^\u5168\u90e8\s*\d+$/.test(t))).toBe(true)
    expect(pills.some((t) => /^All\s*\d+$/.test(t))).toBe(false)
    mount('project-report', 'en')
    const en = Array.from(document.querySelectorAll('button, span')).map((n) => (n.textContent ?? '').trim())
    expect(en.some((t) => /^All\s*\d+$/.test(t))).toBe(true)
  })

  it('names one empty-state probe per built-in', () => {
    expect(Object.keys(PROBES).sort()).toEqual(IDS)
  })

  for (const id of IDS) {
    describe(id, () => {
      it('uses the app catalog words for mate and credits', () => {
        const text = decoded(id)
        for (const [word, means] of Object.entries(OFF_CATALOG)) {
          expect(text.includes(word), `${id} says ${means} as ${word}`).toBe(false)
        }
      })

      it('marks its static words with a key', () => {
        expect(halves(id).body.querySelector('[data-i18n]')).not.toBeNull()
      })

      it('renders every keyed word in zh-CN when the locale is zh-CN', () => {
        const body = mount(id, 'zh-CN')
        const keyed = Array.from(document.querySelectorAll('[data-i18n]'))
        expect(keyed.length).toBeGreaterThan(0)
        for (const node of keyed) {
          expect(node.textContent ?? '', `${id} [data-i18n=${node.getAttribute('data-i18n')}]`).toMatch(CJK)
        }
        const probe = PROBES[id]
        if (probe) {
          expect(body).toContain(probe.zh)
          expect(body).not.toContain(probe.en)
        }
      })

      for (const locale of ['en', 'de', 'xx-YY', undefined]) {
        it(`renders English when the locale is ${String(locale)}`, () => {
          const body = mount(id, locale)
          expect(body).not.toMatch(CJK)
          const probe = PROBES[id]
          if (probe) expect(body).toContain(probe.en)
        })
      }
    })
  }
})
