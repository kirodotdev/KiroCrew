import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Element, Root } from 'hast'
import { rehypeContentDirection } from './contentDirection'

const text = (value: string) => ({ type: 'text' as const, value })
const element = (tagName: string, children: Element['children'], properties: Element['properties'] = {}): Element => ({ type: 'element', tagName, properties, children })

afterEach(() => vi.restoreAllMocks())

function mockNativeDirection(strongDirections: Record<string, 'ltr' | 'rtl'>) {
  const probe = document.createElement('bdi')
  vi.spyOn(probe, 'matches').mockImplementation(selector => {
    const sample = probe.firstChild?.textContent ?? ''
    const fallback = probe.lastChild?.textContent
    const direction = strongDirections[sample] ?? (fallback === '\u200f' ? 'rtl' : 'ltr')
    return selector === `:dir(${direction})`
  })
  vi.spyOn(document, 'createElement').mockReturnValue(probe)
  return probe
}

describe('native content direction resolution', () => {
  it('resolves parent prose from original text before generated child boundaries', () => {
    const probe = mockNativeDirection({ שלום: 'rtl' })
    const link = element('a', [text('שלום')], { href: 'https://example.com/' })
    const paragraph = element('p', [link])
    const quote = element('blockquote', [paragraph])
    const technical = element('code', [text('npm test')])
    const mixed = element('p', [technical, text('שלום')])
    const authored = element('span', [text('English')], { dir: 'ltr' })
    const bounded = element('p', [authored, text('שלום')])
    const tree: Root = { type: 'root', children: [quote, mixed, bounded] }
    rehypeContentDirection()(tree)
    for (const node of [link, paragraph, quote, mixed, bounded]) expect(node.properties.dir).toBe('rtl')
    expect(technical.properties.dir).toBe('ltr')
    expect(authored.properties.dir).toBe('ltr')
    expect(probe.isConnected).toBe(false)
  })

  it('lets a neutral second paragraph inherit the direction of its Hebrew quote', () => {
    const opening = 'שלום.'
    const hours = '09:00 — 10:00'
    mockNativeDirection({ [opening]: 'rtl', [`${opening}\n${hours}`]: 'rtl', 'English.': 'ltr' })
    const hebrew = element('p', [text(opening)])
    const neutral = element('p', [text(hours)])
    const english = element('p', [text('English.')])
    const quote = element('blockquote', [hebrew, text('\n'), neutral])

    rehypeContentDirection()({ type: 'root', children: [quote, english] })

    expect(quote.properties.dir).toBe('rtl')
    expect(neutral.properties).not.toHaveProperty('dir')
    expect(english.properties.dir).toBe('ltr')
  })

  it('lets a nested code-only list item inherit direction while keeping the code LTR', () => {
    const opening = 'הפקודות:\n'
    mockNativeDirection({ [opening]: 'rtl' })
    const code = element('code', [text('npm test')])
    const nested = element('li', [code])
    const item = element('li', [text(opening), element('ul', [nested])])

    rehypeContentDirection()({ type: 'root', children: [element('ul', [item])] })

    expect(item.properties.dir).toBe('rtl')
    expect(nested.properties).not.toHaveProperty('dir')
    expect(code.properties.dir).toBe('ltr')
  })
})
