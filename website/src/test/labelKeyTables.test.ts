/**
 * The label tables that used to hold raw English.
 *
 * Three module-level tables rendered untranslated copy in every locale because
 * `eslint-plugin-i18next` exempts ALL-CAPS declarators, so no gate ever counted
 * them: the session filter rows and sort menu in `ChatSidebar`, and the
 * reasoning-effort levels in `lib/effort.ts` (the `强度 High` in the bug report).
 *
 * The Overview status tiles (`UPTIME` / `SESSIONS` / … above the big numbers) had a
 * different cause with the same symptom: their labels sat in an inline array, so they
 * WERE counted — 6 `object-prop` findings against `pages/OverviewPage.tsx` — and were
 * simply deferred. Converting them here drives that file's ceiling to zero.
 *
 * They now hold catalog KEYS. That is only an improvement if the keys resolve, so
 * this asserts both halves: the key exists in English, and the key is actually
 * translated — a key present in every catalog but left in English would render
 * exactly the bug this replaced.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from '../i18n/index'
import { EFFORT_LABEL_KEY, effortLabel } from '../lib/effort'
import { FILTER_LABEL_KEY, FILTER_DESCRIPTION_KEY, SORT_LABEL_KEY } from '../pages/ChatSidebar'
import { STAT_LABEL_KEY } from '../pages/OverviewPage'
import { NEW_MENU_LABEL_KEY, NEW_MENU_DESC_KEY } from '../pages/chat/SidePanel'
import { STATE_LABEL_KEY } from '../apps/auto-research/ResearchLabPage'

function flatten(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const path = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') Object.assign(out, flatten(value, path))
    else out[path] = String(value)
  }
  return out
}

const en = flatten((CATALOGS.en as { translation: unknown }).translation)
const zh = flatten((CATALOGS['zh-CN'] as { translation: unknown }).translation)

const TABLES: Record<string, Record<string, string>> = {
  EFFORT_LABEL_KEY,
  FILTER_LABEL_KEY,
  FILTER_DESCRIPTION_KEY,
  SORT_LABEL_KEY,
  STAT_LABEL_KEY,
  NEW_MENU_LABEL_KEY,
  NEW_MENU_DESC_KEY,
  STATE_LABEL_KEY,
}

describe('user-visible label tables hold catalog keys', () => {
  for (const [name, table] of Object.entries(TABLES)) {
    it(`${name}: every value is a key that exists in English`, () => {
      const missing = Object.values(table).filter(key => !(key in en))
      expect(missing).toEqual([])
    })

    it(`${name}: every key is translated, not left in English`, () => {
      // `A → Z` and `Z → A` are symbol-led and legitimately identical in zh-CN;
      // anything else identical to English means an untranslated catalog entry.
      const SYMBOLIC = new Set(['pages.chatSidebar.sort_name_asc', 'pages.chatSidebar.sort_name_desc'])
      // "Issues" is a borrowed product noun, not copy: it names GitHub's Issues
      // surface. The shipped zh-CN catalog already leaves it verbatim at
      // `apps.issueRadar.components.issueList.issues`,
      // `apps.issueRadar.components.leftRail.issues` and
      // `components.issuePanel.issues`, all of which predate this table. Demanding
      // a translation here would make the side panel disagree with the three
      // surfaces it navigates to, so the exemption follows the existing catalog
      // rather than inventing a fourth spelling.
      const BORROWED = new Set(['pages.chat.sidePanel.menu_issues'])
      const untranslated = Object.values(table)
        .filter(key => !SYMBOLIC.has(key) && !BORROWED.has(key))
        .filter(key => zh[key] === en[key])
      expect(untranslated).toEqual([])
    })
  }
})

describe('effortLabel', () => {
  it('renders the catalog value for a known level', () => {
    expect(effortLabel('high')).toBe(en['lib.effort.high'])
    expect(effortLabel('')).toBe(en['lib.effort.default'])
  })

  it('does not resolve inherited Object properties as levels', () => {
    // /api/effort-levels is backend-supplied, so a level named `toString` or
    // `constructor` reaches this lookup. A prototype-chain hit would hand a
    // function to i18next instead of a key.
    expect(effortLabel('toString')).toBe('toString')
    expect(effortLabel('constructor')).toBe('constructor')
    expect(effortLabel('hasOwnProperty')).toBe('hasOwnProperty')
  })

  it('returns an unknown level verbatim instead of title-casing it', () => {
    // A level the backend reports via /api/effort-levels that has no catalog
    // entry is an identifier, not copy. It used to be title-cased into fake
    // English (`ultra` -> `Ultra`) in every locale.
    expect(effortLabel('ultra')).toBe('ultra')
  })
})
