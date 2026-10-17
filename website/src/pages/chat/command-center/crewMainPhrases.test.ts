/**
 * The crew main card's phrases, rendered through the catalog (#15684).
 *
 * The English half is pinned against the backend's renderer through the shared fixture
 * `test/fixtures/crew_main_phrases.json`, which `test/test_crew_main_contract.py` reads
 * too: one English, two renderers. The rest pins that every shipped language renders
 * every phrase as its own text, and that anything malformed falls back to the English
 * the backend already wrote rather than to a blank cell or a raw key.
 */
import { describe, it, expect, afterEach } from 'vitest'

import { i18next } from '../../../i18n/all'
import { SUPPORTED_LANGUAGES } from '../../../i18n/languages'
import { __resetFormatterCache } from '../../../i18n/format'
import fixture from '../../../../../test/fixtures/crew_main_phrases.json'
import { CREW_MAIN_PHRASES_FIELD, localizeCrewMainData, renderCrewMainPhrase } from './crewMainPhrases'

type Case = { phrase: unknown[]; english: string }
const CASES = fixture.cases as Case[]

afterEach(async () => {
  await i18next.changeLanguage('en')
  __resetFormatterCache()
})

describe('the English catalog renders what the backend renders', () => {
  it.each(CASES.map(c => [c.english, c] as const))('%s', async (_, c) => {
    await i18next.changeLanguage('en')
    expect(renderCrewMainPhrase(c.phrase)).toBe(c.english)
  })
})

const TRANSLATED = SUPPORTED_LANGUAGES.map(l => l.code).filter(code => code !== 'en' && code !== 'en-XA')

describe('every shipped language translates every phrase', () => {
  it.each(TRANSLATED)('%s', async code => {
    await i18next.changeLanguage(code)
    for (const c of CASES) {
      const text = renderCrewMainPhrase(c.phrase)
      expect(text, `${code} ${JSON.stringify(c.phrase)}`).not.toBeNull()
      expect(text, `${code} ${JSON.stringify(c.phrase)}`).not.toBe(c.english)
      expect(text, `${code} ${JSON.stringify(c.phrase)}`).not.toContain('commandCenter.')
      expect(text, `${code} ${JSON.stringify(c.phrase)}`).not.toMatch(/\{\{/)
    }
  })
})

describe('localizeCrewMainData', () => {
  const english = {
    turns: '12 turns finished',
    agent: 'kirocrew-conductor',
    items_open: '3 of 5 items not yet done',
    lede: 'Working through the queue.',
  }
  const phrases = JSON.stringify({
    turns: ['turns', { n: 12 }],
    items_open: ['items_open', { open: 3, total: 5 }],
  })

  it('renders each phrased field in the active language and drops the phrases key', async () => {
    await i18next.changeLanguage('de')
    const out = localizeCrewMainData({ ...english, [CREW_MAIN_PHRASES_FIELD]: phrases })
    expect(out).toEqual({
      turns: '12 Runden abgeschlossen',
      agent: 'kirocrew-conductor',
      items_open: '3 von 5 Aufgaben noch nicht erledigt',
      lede: 'Working through the queue.',
    })
  })

  it('keeps a turn the backend refused apart from an approval someone rejected', async () => {
    // English says "refused" and "rejected"; a translation must not fold the two
    // events into one word on the same card.
    await i18next.changeLanguage('de')
    expect(renderCrewMainPhrase(['turns_refused', { n: 12, refused: 1 }])).toBe('12 Runden abgeschlossen, 1 vom Backend verweigert')
    expect(renderCrewMainPhrase(['approvals_decided_by', { n: 2, rows: [['rejected', 2]] }])).toBe('2 beantwortet (2 abgelehnt)')
  })

  it('picks the plural form from the count and shows it formatted for the locale', async () => {
    await i18next.changeLanguage('ru')
    expect(renderCrewMainPhrase(['tokens', { n: 1 }])).toBe('измерен 1 токен')
    expect(renderCrewMainPhrase(['tokens', { n: 3 }])).toBe('измерено 3 токена')
    expect(renderCrewMainPhrase(['tokens', { n: 12345 }])).toBe('измерено 12\u00a0345 токенов')
    await i18next.changeLanguage('de')
    expect(renderCrewMainPhrase(['credits', { amount: '1234.50' }])).toBe('1.234,50 Credits dieser Crew berechnet')
  })

  it('names known decisions in the breakdown and joins them for the locale', async () => {
    await i18next.changeLanguage('ja')
    expect(renderCrewMainPhrase(['approvals_decided_by', { n: 4, rows: [['approved', 3], ['rejected_once', 1]] }]))
      .toBe('回答済み 4 件（承認 3 件、今回のみ拒否 1 件）')
  })

  it('returns a card with no phrases unchanged', () => {
    expect(localizeCrewMainData(english)).toBe(english)
  })

  it.each([
    ['not JSON', '{'],
    ['an array', '[]'],
    ['an unknown id', JSON.stringify({ turns: ['turns_forever', { n: 1 }] })],
    ['a missing param', JSON.stringify({ items_open: ['items_open', { open: 3 }] })],
    ['an extra param', JSON.stringify({ turns: ['turns', { n: 1, extra: 2 }] })],
    ['a negative count', JSON.stringify({ turns: ['turns', { n: -1 }] })],
    ['a fractional count', JSON.stringify({ turns: ['turns', { n: 1.5 }] })],
    ['a count sent as text', JSON.stringify({ turns: ['turns', { n: '12' }] })],
    ['an amount not to two places', JSON.stringify({ credits: ['credits', { amount: '3.5' }] })],
    ['a decision the catalog cannot name', JSON.stringify({ approvals_decided: ['approvals_decided_by', { n: 7, rows: [['allow', 6], ['deny', 1]] }] })],
    ['an inherited key as the id', JSON.stringify({ turns: ['toString'] })],
  ])('keeps the English for %s', async (_, raw) => {
    await i18next.changeLanguage('fr')
    const data = { ...english, credits: '3.50 credits billed to this crew', approvals_decided: '7 answered (6 allow, 1 deny)' }
    const out = localizeCrewMainData({ ...data, [CREW_MAIN_PHRASES_FIELD]: raw })
    expect(out).toEqual(data)
  })

  it('never adds a field the card does not carry', async () => {
    await i18next.changeLanguage('es')
    const raw = JSON.stringify({ tokens: ['tokens', { n: 5 }], turns: ['turns', { n: 12 }] })
    const out = localizeCrewMainData({ ...english, [CREW_MAIN_PHRASES_FIELD]: raw })
    expect(Object.keys(out).sort()).toEqual(Object.keys(english).sort())
    expect(out.turns).toBe('12 turnos terminados')
  })
})
