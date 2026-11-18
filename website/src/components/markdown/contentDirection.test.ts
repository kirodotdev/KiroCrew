import { afterEach, describe, expect, it, vi } from 'vitest'
import type { Element, Root } from 'hast'
import { rehypeContentDirection } from './contentDirection'

const text = (value: string) => ({ type: 'text' as const, value })
const element = (tagName: string, children: Element['children'], properties: Element['properties'] = {}): Element => ({ type: 'element', tagName, properties, children })

afterEach(() => vi.restoreAllMocks())

describe('native content direction resolution', () => {
  it('resolves parent prose from original text before generated child boundaries', () => {
    const probe = document.createElement('bdi')
    const samples: string[] = []
    vi.spyOn(probe, 'matches').mockImplementation(selector => {
      if (selector === ':dir(ltr)') return true
      samples.push(probe.textContent ?? '')
      return probe.textContent === 'שלום'
    })
    vi.spyOn(document, 'createElement').mockReturnValue(probe)
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
    expect(samples.every(sample => sample === 'שלום')).toBe(true)
    expect(probe.isConnected).toBe(false)
  })
})
