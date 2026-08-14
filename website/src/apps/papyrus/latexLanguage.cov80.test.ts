/**
 * registerLatexLanguage — the Monaco LaTeX/BibTeX registration.
 *
 * Three properties are worth pinning: it registers the id with the extensions
 * Papyrus opens, it is idempotent (a second call must not re-register), and it
 * NEVER throws out to the caller — a failure costs highlighting only, because
 * an unregistered id makes Monaco fall back to plaintext.
 *
 * The module keeps a `registered` flag at module scope, so each test re-imports
 * it through `vi.resetModules()` to get a clean slate.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type { Monaco } from '@monaco-editor/react'
import type { languages } from 'monaco-editor'
import { LATEX_LANGUAGE_ID } from './lib'

interface FakeMonaco {
  monaco: Monaco
  register: ReturnType<typeof vi.fn>
  setLanguageConfiguration: ReturnType<typeof vi.fn>
  setMonarchTokensProvider: ReturnType<typeof vi.fn>
}

function fakeMonaco(opts: { existing?: string[]; throwOnList?: boolean } = {}): FakeMonaco {
  const register = vi.fn()
  const setLanguageConfiguration = vi.fn()
  const setMonarchTokensProvider = vi.fn()
  const getLanguages = vi.fn(() => {
    if (opts.throwOnList) throw new Error('zzq monaco unavailable')
    return (opts.existing ?? []).map((id) => ({ id }))
  })
  const monaco = {
    languages: { getLanguages, register, setLanguageConfiguration, setMonarchTokensProvider },
  } as unknown as Monaco
  return { monaco, register, setLanguageConfiguration, setMonarchTokensProvider }
}

async function freshRegister() {
  vi.resetModules()
  const mod = await import('./latexLanguage')
  return mod.registerLatexLanguage
}

beforeEach(() => {
  vi.resetModules()
})

describe('registerLatexLanguage', () => {
  it('registers the id with the Papyrus source extensions and a % line comment', async () => {
    const registerLatexLanguage = await freshRegister()
    const m = fakeMonaco()
    registerLatexLanguage(m.monaco)

    expect(m.register).toHaveBeenCalledTimes(1)
    expect(m.register.mock.calls[0][0]).toMatchObject({
      id: LATEX_LANGUAGE_ID,
      extensions: ['.tex', '.sty', '.cls', '.bib'],
    })
    const [id, config] = m.setLanguageConfiguration.mock.calls[0]
    expect(id).toBe(LATEX_LANGUAGE_ID)
    expect((config as languages.LanguageConfiguration).comments).toEqual({ lineComment: '%' })
  })

  it('installs a tokenizer with the math sub-states the root rules push into', async () => {
    const registerLatexLanguage = await freshRegister()
    const m = fakeMonaco()
    registerLatexLanguage(m.monaco)

    const monarch = m.setMonarchTokensProvider.mock.calls[0][1] as languages.IMonarchLanguage
    expect(Object.keys(monarch.tokenizer)).toEqual(['root', 'inlineMath', 'displayMath'])
    expect(monarch.defaultToken).toBe('')
  })

  it('treats an escaped percent as literal text, not the start of a comment', async () => {
    const registerLatexLanguage = await freshRegister()
    const m = fakeMonaco()
    registerLatexLanguage(m.monaco)

    const monarch = m.setMonarchTokensProvider.mock.calls[0][1] as languages.IMonarchLanguage
    const commentRule = (monarch.tokenizer.root as unknown[])[0] as [RegExp, unknown]
    expect(commentRule[0].test('x % zzq trailing note')).toBe(true)
    expect(commentRule[0].test('99\\% of the runs')).toBe(false)
  })

  it('is idempotent: a second call re-registers nothing', async () => {
    const registerLatexLanguage = await freshRegister()
    const first = fakeMonaco()
    registerLatexLanguage(first.monaco)
    const second = fakeMonaco()
    registerLatexLanguage(second.monaco)

    expect(second.register).not.toHaveBeenCalled()
    expect(second.setMonarchTokensProvider).not.toHaveBeenCalled()
  })

  it('skips registration when Monaco already knows the id', async () => {
    const registerLatexLanguage = await freshRegister()
    const m = fakeMonaco({ existing: ['plaintext', LATEX_LANGUAGE_ID] })
    registerLatexLanguage(m.monaco)

    expect(m.register).not.toHaveBeenCalled()
    expect(m.setLanguageConfiguration).not.toHaveBeenCalled()
  })

  it('swallows a Monaco failure and stays retryable', async () => {
    const registerLatexLanguage = await freshRegister()
    const broken = fakeMonaco({ throwOnList: true })
    expect(() => registerLatexLanguage(broken.monaco)).not.toThrow()
    expect(broken.register).not.toHaveBeenCalled()

    // The flag is rolled back on failure, so a later mount can still register.
    const working = fakeMonaco()
    registerLatexLanguage(working.monaco)
    expect(working.register).toHaveBeenCalledTimes(1)
  })
})
