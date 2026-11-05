/**
 * The Design Critique page's own text under a non-English language.
 *
 * The empty list, the "What you'll get" list, a history entry's screen count,
 * a step's finding count, the screen fallback name, the failure toast and the
 * ask-error lines were English literals, or a count glued to an English noun.
 * These mount the real page in Russian and read what a user would read; each
 * expected value comes from the catalog and is checked against English first,
 * so a missing key cannot pass by falling back.
 */
import fs from 'node:fs'
import path from 'node:path'
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'

// `/all` registers every authored catalog; `../i18n` registers English only.
import { i18next } from '../../i18n/all'
import { HKEY } from './constants'
import type { HistoryEntry } from './types'

vi.mock('./api', () => ({
  designCritiqueApi: {
    openSlot: vi.fn(), getSlot: vi.fn(), send: vi.fn(), deleteSlot: vi.fn(), uploadFiles: vi.fn(),
    discover: vi.fn(), render: vi.fn(), pollDiscover: vi.fn(), pollRender: vi.fn(),
    method: vi.fn().mockResolvedValue({ skill: '', checklist: '' }),
  },
  fileUrl: (p: string) => '/api/file-raw?path=' + encodeURIComponent(p),
}))

const DesignCritiquePage = (await import('./DesignCritiquePage')).default

const K = 'apps.designCritique.designCritiquePage.'
const SRC = path.resolve(__dirname, '..', '..')

/** The `lng` value of `key`, guarded so a missing translation fails here. */
function tr(lng: string, key: string, opts?: Record<string, unknown>): string {
  const en = i18next.getFixedT('en')(K + key, opts) as string
  const value = i18next.getFixedT(lng)(K + key, opts) as string
  expect(value).not.toBe(en)
  expect(value).not.toBe(K + key)
  return value
}

function entry(id: number): HistoryEntry {
  return {
    id,
    // A fixed instant: the list shows a relative time, which this reads by prefix only.
    ts: 1_790_000_000_000 - id * 1000,
    slotKey: 'slot-' + id,
    screens: [
      { step: 1, label: 'Cart', url: '/api/file-raw?path=%2Ftmp%2Fa.png' },
      { step: 2, label: 'Pay', url: '/api/file-raw?path=%2Ftmp%2Fb.png' },
    ],
    thumbUrl: '/api/file-raw?path=%2Ftmp%2Fa.png',
    read: 'Critique number ' + id,
    report: { overallRead: 'Critique number ' + id, findings: [] },
  } as unknown as HistoryEntry
}

beforeEach(() => { localStorage.clear() })
afterEach(async () => {
  cleanup()
  await i18next.changeLanguage('en')
})
afterAll(async () => { await i18next.changeLanguage('en') })

describe('Design Critique page text', () => {
  it('shows the empty list in Russian', async () => {
    await i18next.changeLanguage('ru')
    render(<DesignCritiquePage />)
    const empty = [tr('ru', 'no_critiques_narrow'), tr('ru', 'no_critiques_wide')]
    expect(screen.getByText((t) => empty.includes(t))).toBeInTheDocument()
  })

  it('has every new line in each shipped language', () => {
    const keys = ['get_overall_read', 'get_whats_working', 'get_top_things', 'critique_ready_fallback',
      'ask_too_long', 'ask_went_wrong', 'nothing_flagged', 'design_critique_fallback', 'the_design_alt']
    for (const lng of ['de', 'es', 'fr', 'it', 'pt', 'ru', 'ja', 'ko', 'zh-CN', 'hi', 'bn']) {
      for (const key of keys) tr(lng, key)
      tr(lng, 'screen_n', { n: '2' })
    }
  })

  it('puts the screen count inside a Russian history line', async () => {
    await i18next.changeLanguage('ru')
    localStorage.setItem(HKEY, JSON.stringify([entry(1)]))
    render(<DesignCritiquePage />)
    expect(screen.getByText(/^2 экрана · /)).toBeInTheDocument()
  })

  it('counts findings with the Russian plural forms', () => {
    expect(tr('ru', 'findings_count', { count: 1, n: '1' })).toBe('1 замечание')
    expect(tr('ru', 'findings_count', { count: 3, n: '3' })).toBe('3 замечания')
    expect(tr('ru', 'findings_count', { count: 5, n: '5' })).toBe('5 замечаний')
    expect(tr('ru', 'flow_screens_count', { count: 5, n: '5' })).toBe('5 экранов')
  })

  it.each([
    "' finding' : ' findings'",
    "'nothing flagged'",
    "'No critiques yet.'",
    "'An overall read of how it’s doing'",
    "n + ' screens · '",
    "' screens' : ''",
    "'Critique failed: '",
    "'That took too long — ask again.'",
    "'Something went wrong.'",
    "|| 'Screen ' + (i + 1)",
    "'the design'",
    "'Design critique'",
    "'Critique ready'",
  ])('DesignCritiquePage.tsx takes its text from the catalog (%s)', (literal) => {
    const src = fs.readFileSync(path.join(SRC, 'apps/design-critique/DesignCritiquePage.tsx'), 'utf8')
    expect(src).not.toContain(literal)
  })
})
