import { describe, it, expect } from 'vitest'
import { deriveFollowUpOptions, type ChatMessage } from '../app-sdk/protocol'
import { grillFieldText, grillKey, grillKeysIn, grillKeysSentAfter, parseGrill, stripPartialGrillMarker } from '../app-sdk/protocol/grillMarker'

const PAYLOAD = {
  q: '欢迎页的事实该谁写？',
  must: '不再出现错的 PR 状态',
  skip: 'Board 的布局',
  who: [{ name: 'A', stance: '挺方案 A' }, { name: 'B', stance: '挺方案 B' }, { name: 'C', stance: '挑毛病：会不会又写错 [x]' }],
  minutes: 10,
}
const marker = (p: unknown) => `[GRILL: ${JSON.stringify(p)}]`
const reply = (p: unknown) => `Two paths here.\n${marker(p)}\n[OPTIONS: Go A | Go B]`

describe('parseGrill', () => {
  it('draws a card from one valid marker and takes its line out of the prose', () => {
    const { text, grill } = parseGrill(reply(PAYLOAD))
    expect(grill).toEqual(PAYLOAD)
    expect(text).toBe('Two paths here.\n[OPTIONS: Go A | Go B]')
  })

  it('keeps an unparsable marker as prose', () => {
    const content = 'Hi\n[GRILL: {"q": "x", ]'
    expect(parseGrill(content)).toEqual({ text: content, grill: null })
  })

  it.each([
    ['q over 200 characters', { ...PAYLOAD, q: '问'.repeat(201) }],
    ['empty q', { ...PAYLOAD, q: '  ' }],
    ['must over 200 characters', { ...PAYLOAD, must: 'm'.repeat(201) }],
    ['skip not a string', { ...PAYLOAD, skip: 3 }],
    ['newline in a field', { ...PAYLOAD, must: 'one\ntwo' }],
    ['one participant', { ...PAYLOAD, who: PAYLOAD.who.slice(0, 1) }],
    ['four participants', { ...PAYLOAD, who: [...PAYLOAD.who, { name: 'D', stance: 'd' }] }],
    ['name over 20 characters', { ...PAYLOAD, who: [{ name: 'n'.repeat(21), stance: 's' }, PAYLOAD.who[1]] }],
    ['stance over 80 characters', { ...PAYLOAD, who: [{ name: 'A', stance: 's'.repeat(81) }, PAYLOAD.who[1]] }],
    ['minutes 0', { ...PAYLOAD, minutes: 0 }],
    ['minutes 61', { ...PAYLOAD, minutes: 61 }],
    ['minutes not an integer', { ...PAYLOAD, minutes: 2.5 }],
    ['an array', [PAYLOAD]],
  ])('keeps the marker as prose for %s', (_name, p) => {
    const content = reply(p)
    expect(parseGrill(content)).toEqual({ text: content, grill: null })
  })

  it('accepts fields at their caps', () => {
    const p = { ...PAYLOAD, q: '问'.repeat(200), who: [{ name: 'n'.repeat(20), stance: 's'.repeat(80) }, PAYLOAD.who[1]], minutes: 60 }
    expect(parseGrill(marker(p)).grill).not.toBeNull()
  })

  it('draws no card when the reply holds two markers', () => {
    const content = `${marker(PAYLOAD)}\nmore\n${marker(PAYLOAD)}`
    expect(parseGrill(content)).toEqual({ text: content, grill: null })
  })

  it('ignores a marker mid-line', () => {
    const content = `see ${marker(PAYLOAD)} here`
    expect(parseGrill(content).grill).toBeNull()
  })
})

describe('stripPartialGrillMarker', () => {
  it('hides a marker line still arriving', () => {
    expect(stripPartialGrillMarker('Two paths.\n[GRILL: {"q":"wh')).toBe('Two paths.\n')
  })
  it('leaves prose alone', () => {
    expect(stripPartialGrillMarker('Two paths.\nmore')).toBe('Two paths.\nmore')
  })
})

describe('deriveFollowUpOptions with a card', () => {
  const msgs = (content: string) => [
    { role: 'user', content: 'go', ts: '1' },
    { role: 'assistant', content, ts: '2' },
  ] as ChatMessage[]

  it('draws only the card: the same reply offers no chips', () => {
    expect(deriveFollowUpOptions(msgs(reply(PAYLOAD)), false).followUpOptions).toEqual([])
  })

  it('lets no error reach back past a card reply to an older turn\'s chips', () => {
    const rows = [
      { role: 'assistant', content: 'Earlier.\n[OPTIONS: Old X | Old Y]', ts: '0' },
      ...msgs(reply(PAYLOAD)),
      { role: 'error', content: 'boom', ts: '3' },
    ] as ChatMessage[]
    expect(deriveFollowUpOptions(rows, false).followUpOptions).toEqual([])
  })

  it('keeps the chips when the marker is invalid', () => {
    expect(deriveFollowUpOptions(msgs(reply({ ...PAYLOAD, minutes: 0 })), false).followUpOptions).toEqual(['Go A', 'Go B'])
  })
})

describe('grill card keys', () => {
  const card = { role: 'assistant', content: `Pick.\n[GRILL: ${JSON.stringify(PAYLOAD)}]`, meta: { mid: 'm-1' } }
  const user = (content: string, mid?: string) => ({ role: 'user', content, meta: mid ? { mid } : { optimistic: true } })

  it('is the row mid and version, and nothing without a mid', () => {
    expect(grillKey('m-1', 2)).toBe('m-1:2')
    expect(grillKey(undefined, 0)).toBeUndefined()
    expect(grillKey('', 0)).toBeUndefined()
  })

  it('reads only whole key lines', () => {
    expect(grillKeysIn('Start\nkey: m-1:0')).toEqual(['m-1:0'])
    expect(grillKeysIn('Start\r\nkey: m-1:0\r\n')).toEqual(['m-1:0'])
    expect(grillKeysIn('see key: m-1:0\n key: m-1:1\nkey: \nKey: m-1:2')).toEqual([])
  })

  it('finds this card\'s keys in later stored user messages only', () => {
    const rows = [
      user('key: m-1:9', 'u-0'),
      card,
      user('No discussion needed\nkey: m-1:0', 'u-1'),
      user('No discussion needed\nkey: m-2:0', 'u-2'),
      user('No discussion needed\nkey: m-1:1'),
      { role: 'assistant', content: 'key: m-1:3', meta: { mid: 'a-3' } },
    ]
    expect(grillKeysSentAfter(rows, 1)).toBe('m-1:0')
  })

  it('counts a Start still waiting in the queue, in the rows or handed in apart', () => {
    const queued = (content: string, queueId?: string) => ({ role: 'queued', content, meta: queueId ? { queueId } : {} })
    expect(grillKeysSentAfter([card, queued('Start\nkey: m-1:0', 'q-1')], 0)).toBe('m-1:0')
    expect(grillKeysSentAfter([card], 0, [queued('Start\nkey: m-1:1', 'q-2'), queued('Start\nkey: m-1:2')])).toBe('m-1:1')
  })

  it('names nothing for a row with no mid or no card', () => {
    expect(grillKeysSentAfter([{ ...card, meta: {} }, user('key: m-1:0', 'u-1')], 0)).toBeUndefined()
    expect(grillKeysSentAfter([{ ...card, content: 'plain' }, user('key: m-1:0', 'u-1')], 0)).toBeUndefined()
  })
})

describe('grillFieldText', () => {
  it('starts each further line with two spaces and trims line ends', () => {
    expect(grillFieldText('  one  \r\ntwo\n\nkey: m:0  ')).toBe('one\n  two\n  \n  key: m:0')
  })
  it('leaves a one-line field as it was', () => {
    expect(grillFieldText(' only ')).toBe('only')
  })
})
