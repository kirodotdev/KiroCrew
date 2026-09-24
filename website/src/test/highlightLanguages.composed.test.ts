// End-to-end check of the edition languages seam with an edition module in
// place: the virtual module is mocked the way an edition's languages.ts would
// resolve, then every consumer is asked about the contributed language.
import { describe, it, expect, vi } from 'vitest'

vi.mock('virtual:kirocrew-edition-languages', () => ({
  default: [
    {
      id: 'demo',
      aliases: ['dmo'],
      extensions: ['.demo'],
      hljs: () => ({ name: 'Demo', keywords: { keyword: 'demo' } }),
      textmate: () =>
        Promise.resolve({
          default: [
            {
              name: 'demo',
              scopeName: 'source.demo',
              patterns: [{ match: '\\bdemo\\b', name: 'keyword.control.demo' }],
              repository: {},
            },
          ],
        }),
    },
    {
      id: 'other',
      extensions: ['.odd'],
      textmate: () =>
        Promise.resolve({
          default: [{ name: 'other', scopeName: 'source.other', patterns: [], repository: {} }],
        }),
    },
  ],
}))

describe('edition languages — composed', () => {
  it('resolves fence tags by id, alias and extension; core tags are unchanged', async () => {
    const { fenceLanguage } = await import('../pierre/PierreImpl')
    expect(fenceLanguage('demo')).toBe('demo')
    expect(fenceLanguage('DMO')).toBe('demo')
    expect(fenceLanguage('python')).toBe('python')
    expect(fenceLanguage('nope')).toBe('text')
  })

  it('passes contributed extensions to Pierre as their token, which resolves to the language', async () => {
    const { langFor } = await import('../components/ContentRenderer')
    const { fenceLanguage } = await import('../pierre/PierreImpl')
    expect(langFor('.demo')).toBe('demo')
    expect(langFor('.odd')).toBe('odd')
    expect(fenceLanguage(langFor('.odd'))).toBe('other')
    expect(langFor('.ts')).toBe('typescript')
    expect(langFor('.unknown')).toBe('plaintext')
  })

  it('makes the grammar resolvable where the worker pool resolves languages', async () => {
    await import('../pierre/PierreImpl')
    const { resolveLanguage, getFiletypeFromFileName } = await import('@pierre/diffs')
    const resolved = await resolveLanguage('demo')
    expect(resolved.name).toBe('demo')
    expect(getFiletypeFromFileName('x.demo')).toBe('demo')
  })

  it('registers the hljs grammar for the worker and main-thread instances', async () => {
    const { default: hljs } = await import('../utils/hljs')
    expect(hljs.getLanguage('dmo')).toBeDefined()
  })
})
