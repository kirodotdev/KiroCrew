/**
 * The generic noun "crew" is written in the catalog's own script in Bengali,
 * Hindi, Japanese, Korean, Simplified Chinese and Russian, not left in Latin
 * letters beside the native word (#37161, #36383). Each of these catalogs words
 * it one way (ক্রু, क्रू, クルー, 크루, 团队, команда).
 *
 * What stays Latin and is not counted here: the product names, the
 * `~/.kiro/crew` data path, `{{placeholders}}`, code in backticks, the
 * capitalised "Crew" (the name of one install), and the English menu and
 * setting labels a sentence quotes verbatim.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

const LOCALES = ['bn', 'hi', 'ja', 'ko', 'zh-CN', 'ru']

const VERBATIM = [
  'Kiro Crew',
  'Crew Companion',
  '~/.kiro/crew',
  'New chat on crew',
  'Set Remote Host…',
  'Keep an SSH tunnel to this crew open',
]

function blank(text: string, open: string, close: string): string {
  let out = text
  for (let from = out.indexOf(open); from >= 0; from = out.indexOf(open)) {
    const end = out.indexOf(close, from + open.length)
    if (end < 0) break
    out = out.slice(0, from) + ' ' + out.slice(end + close.length)
  }
  return out
}

function isWordChar(c: string): boolean {
  return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c === '_'
}

/** Words made of ASCII letters, digits and underscores; every other character separates. */
function latinWords(text: string): string[] {
  const words: string[] = []
  let current = ''
  for (const c of text) {
    if (isWordChar(c)) current += c
    else if (current) {
      words.push(current)
      current = ''
    }
  }
  if (current) words.push(current)
  return words
}

function latinCrewWords(value: string): string[] {
  let text = blank(blank(value, '{{', '}}'), '`', '`')
  for (const phrase of VERBATIM) text = text.split(phrase).join(' ')
  return latinWords(text).filter(w => w === 'crew' || w === 'crews')
}

function strings(node: unknown, path: string, out: Array<[string, string]>): void {
  if (typeof node === 'string') out.push([path, node])
  else if (typeof node === 'object' && node !== null)
    for (const [k, v] of Object.entries(node)) strings(v, path ? `${path}.${k}` : k, out)
}

describe('the generic noun "crew" is native-script in bn, hi, ja, ko, zh-CN and ru', () => {
  it('finds a Latin "crew" in an English-looking value (detector self-check)', () => {
    expect(latinCrewWords('Issue Radar で crew を設定')).toEqual(['crew'])
    expect(latinCrewWords('Настройте crews в Issue Radar')).toEqual(['crews'])
    expect(latinCrewWords('{{crew}} Kiro Crew ~/.kiro/crew `crew` “New chat on crew” KIROCREW_CREW_LOG Crew')).toEqual([])
  })

  it.each(LOCALES)('%s: no value spells the generic noun in Latin letters', code => {
    const catalog = (CATALOGS[code] as { translation: Record<string, unknown> }).translation
    const all: Array<[string, string]> = []
    strings(catalog, '', all)
    const offenders = all.filter(([, value]) => latinCrewWords(value).length > 0).map(([key]) => key)
    expect(offenders).toEqual([])
  })
})
