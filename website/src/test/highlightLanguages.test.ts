import { describe, it, expect, vi } from 'vitest'
import hljs from 'highlight.js/lib/core'
import type { LanguageRegistration } from '@pierre/diffs'
import {
  HIGHLIGHT_LANGUAGES,
  highlightLanguageForExtension,
  highlightLanguageForTag,
  validateHighlightLanguages,
  type HighlightLanguageContribution,
} from '../utils/highlightLanguages'
import { registerHljsLanguages } from '../utils/hljsLanguages'
import { registerEditionShikiLanguages } from '../pierre/PierreImpl'

const DEMO_HLJS: HighlightLanguageContribution['hljs'] = () => ({
  name: 'Demo',
  keywords: { keyword: 'demo' },
})
const DEMO_TEXTMATE: LanguageRegistration = {
  name: 'demo',
  scopeName: 'source.demo',
  patterns: [{ match: '\\bdemo\\b', name: 'keyword.control.demo' }],
  repository: {},
}
const demo = (over: Partial<HighlightLanguageContribution> = {}): HighlightLanguageContribution => ({
  id: 'demo',
  aliases: ['dmo'],
  extensions: ['.DEMO'],
  hljs: DEMO_HLJS,
  textmate: () => Promise.resolve({ default: [DEMO_TEXTMATE] }),
  ...over,
})

describe('highlightLanguages — edition language seam', () => {
  it('contributes nothing in the stock build', () => {
    expect(HIGHLIGHT_LANGUAGES).toEqual([])
  })

  it('normalizes a valid contribution and resolves it by tag and extension', () => {
    const [lang] = validateHighlightLanguages([demo()])
    expect(lang.aliases).toEqual(['dmo'])
    expect(lang.extensions).toEqual(['.demo'])
    expect(highlightLanguageForTag('DMO', [lang])?.id).toBe('demo')
    expect(highlightLanguageForExtension('.demo', [lang])?.id).toBe('demo')
    expect(highlightLanguageForExtension('.other', [lang])).toBeUndefined()
  })

  it('rejects a malformed id, a missing grammar, a bad extension and a repeated name', () => {
    expect(() => validateHighlightLanguages([demo({ id: 'Demo Lang' })])).toThrow(/must match/)
    expect(() => validateHighlightLanguages([demo({ hljs: undefined, textmate: undefined })])).toThrow(/neither/)
    expect(() => validateHighlightLanguages([demo({ extensions: ['demo'] })])).toThrow(/start with a dot/)
    expect(() => validateHighlightLanguages([demo(), demo({ id: 'other', aliases: ['demo'] })])).toThrow(
      /already contributed/,
    )
  })

  it('reports a non-array export or a malformed entry instead of throwing a TypeError', () => {
    expect(() => validateHighlightLanguages({ default: [] })).toThrow(/readable array/)
    expect(() => validateHighlightLanguages([null])).toThrow(/not an object/)
    expect(() => validateHighlightLanguages([{ id: 3 }])).toThrow(/no string id/)
    expect(() => validateHighlightLanguages([demo({ aliases: 'dmo' as never })])).toThrow(/string array/)
    expect(() => validateHighlightLanguages([demo({ textmate: {} as never })])).toThrow(/must be functions/)
  })

  it('degrades to an empty list with a warning in production', () => {
    vi.stubEnv('DEV', false)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      expect(validateHighlightLanguages(undefined)).toEqual([])
      expect(validateHighlightLanguages([42, { id: 'x', extensions: 7 }, demo()]).map(l => l.id)).toEqual(['demo'])
      expect(warn).toHaveBeenCalledTimes(3)

      const throwingEntry = Object.defineProperty({}, 'id', {
        enumerable: true,
        get() {
          throw new TypeError('boom')
        },
      })
      const throwingList = new Proxy([demo()], {
        get() {
          throw new TypeError('boom')
        },
      })
      expect(validateHighlightLanguages([throwingEntry, demo()]).map(l => l.id)).toEqual(['demo'])
      expect(validateHighlightLanguages(throwingList)).toEqual([])
      expect(warn).toHaveBeenCalledTimes(5)
    } finally {
      vi.unstubAllEnvs()
      warn.mockRestore()
    }
  })
})

describe('registerHljsLanguages — edition languages', () => {
  it('registers a contributed grammar and its aliases after the core set', () => {
    const instance = hljs.newInstance()
    registerHljsLanguages(instance, validateHighlightLanguages([demo()]))
    expect(instance.getLanguage('demo')).toBeDefined()
    expect(instance.getLanguage('dmo')).toBeDefined()
    expect(instance.highlight('demo x', { language: 'dmo' }).value).toContain('hljs-keyword')
  })

  it('keeps a core language when a contribution claims its name', () => {
    const instance = hljs.newInstance()
    expect(() => registerHljsLanguages(instance, validateHighlightLanguages([demo({ id: 'python' })]))).toThrow(
      /core language/,
    )
  })

  it('never hands highlight.js or Pierre the edition function object itself', () => {
    const grammar = Object.defineProperty(() => ({ name: 'Demo' }), 'bind', {
      get() {
        throw new TypeError('boom')
      },
    })
    const [lang] = validateHighlightLanguages([demo({ hljs: grammar as never, textmate: grammar as never })])
    expect(lang.hljs).not.toBe(grammar)
    expect(lang.textmate).not.toBe(grammar)
    const instance = hljs.newInstance()
    expect(() => registerHljsLanguages(instance, [lang])).not.toThrow()
    expect(instance.getLanguage('demo')).toBeDefined()
  })
})

describe('registerEditionShikiLanguages — Pierre/Shiki', () => {
  it('registers the TextMate loader with dot-less extensions', () => {
    const register = vi.fn()
    const registered = registerEditionShikiLanguages(validateHighlightLanguages([demo()]), register)
    expect([...registered]).toEqual(['demo'])
    expect(register).toHaveBeenCalledWith('demo', expect.any(Function), ['demo'])
  })

  it('skips a contribution with no TextMate grammar', () => {
    const register = vi.fn()
    registerEditionShikiLanguages(validateHighlightLanguages([demo({ textmate: undefined })]), register)
    expect(register).not.toHaveBeenCalled()
  })

  it('keeps core Shiki names and extensions', () => {
    const register = vi.fn()
    expect(() => registerEditionShikiLanguages(validateHighlightLanguages([demo({ id: 'python' })]), register)).toThrow(
      /core language/,
    )
    expect(() =>
      registerEditionShikiLanguages(validateHighlightLanguages([demo({ extensions: ['.py'] })]), register),
    ).toThrow(/core extension/)
    expect(() =>
      registerEditionShikiLanguages(validateHighlightLanguages([demo({ extensions: ['.console'] })]), register),
    ).toThrow(/core extension or language/)
    expect(() =>
      registerEditionShikiLanguages(validateHighlightLanguages([demo({ aliases: ['dm'] })]), register),
    ).toThrow(/core language/)
    expect(register).not.toHaveBeenCalled()
  })

  it("rejects Pierre's reserved text and ansi ids instead of letting registration throw", () => {
    const register = vi.fn()
    for (const id of ['text', 'ansi']) {
      expect(() => registerEditionShikiLanguages(validateHighlightLanguages([demo({ id })]), register)).toThrow(
        /core language/,
      )
    }
    expect(register).not.toHaveBeenCalled()
  })
})
