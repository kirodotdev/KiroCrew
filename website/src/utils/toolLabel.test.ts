import { describe, it, expect } from 'vitest'
import { labelMatchesLanguage, pickToolLabel } from './toolLabel'

describe('labelMatchesLanguage', () => {
  it('accepts same-script prose', () => {
    expect(labelMatchesLanguage('Reading the config file', 'en')).toBe(true)
    expect(labelMatchesLanguage('获取第一个 Figma 节点的设计上下文', 'zh-CN')).toBe(true)
    expect(labelMatchesLanguage('스크린샷 캡처', 'ko')).toBe(true)
    expect(labelMatchesLanguage('Проверка статуса шлюза', 'ru')).toBe(true)
  })

  it('rejects foreign hard-script prose under a Latin UI (the reported bug)', () => {
    expect(labelMatchesLanguage('获取第一个 Figma 节点的设计上下文', 'en')).toBe(false)
    expect(labelMatchesLanguage('查看第二个节点的视觉设计', 'en')).toBe(false)
    expect(labelMatchesLanguage('스크린샷 캡처', 'en')).toBe(false)
    expect(labelMatchesLanguage('Проверка статуса', 'de')).toBe(false)
  })

  it('rejects Latin-only prose under a non-Latin UI (reverse direction)', () => {
    expect(labelMatchesLanguage('Reading the config file', 'zh-CN')).toBe(false)
    expect(labelMatchesLanguage('Capture screenshot', 'ko')).toBe(false)
  })

  it('keeps a purpose that mixes an English identifier into native prose', () => {
    // A Latin tool/id token inside otherwise-Chinese prose still carries Han
    // characters, so it must NOT be treated as a mismatch under a Chinese UI.
    expect(labelMatchesLanguage('调用 figma_get_design_context 工具', 'zh-CN')).toBe(true)
    // ...and the same string under an English UI is a mismatch (has Han).
    expect(labelMatchesLanguage('调用 figma_get_design_context 工具', 'en')).toBe(false)
  })

  it('treats truly neutral text (digits, punctuation, empty) as compatible everywhere', () => {
    expect(labelMatchesLanguage('12345', 'ko')).toBe(true)
    expect(labelMatchesLanguage('12345', 'en')).toBe(true)
    expect(labelMatchesLanguage('- • / :', 'zh-CN')).toBe(true)
    expect(labelMatchesLanguage('', 'en')).toBe(true)
  })

  it('a Latin path/identifier passes under a Latin UI but not a non-Latin one', () => {
    expect(labelMatchesLanguage('/tmp/ghosts/p1.svg', 'en')).toBe(true)
    // Under a Chinese UI an all-Latin string is the wrong writing system, so
    // the caller falls back to the (also-Latin) raw tool label — no worse, and
    // a correctly-written Chinese purpose always carries Han characters.
    expect(labelMatchesLanguage('/tmp/ghosts/p1.svg', 'zh-CN')).toBe(false)
  })

  it('matches on the primary subtag, ignoring region', () => {
    expect(labelMatchesLanguage('查看节点', 'zh-TW')).toBe(true)
    expect(labelMatchesLanguage('Reading file', 'pt-BR')).toBe(true)
  })

  it('falls back to Latin for unknown tags (only suppresses clear foreign script)', () => {
    expect(labelMatchesLanguage('Reading file', 'xx')).toBe(true)
    expect(labelMatchesLanguage('获取节点', 'xx')).toBe(false)
  })
})

describe('pickToolLabel', () => {
  const rawLabel = 'Running: figma_get_design_context'

  it('returns the raw label when simplified names are off', () => {
    expect(pickToolLabel({ simplified: false, purpose: 'Fetch the node', rawLabel, uiLang: 'en' })).toBe(rawLabel)
  })

  it('returns the raw label when there is no purpose', () => {
    expect(pickToolLabel({ simplified: true, purpose: '', rawLabel, uiLang: 'en' })).toBe(rawLabel)
    expect(pickToolLabel({ simplified: true, purpose: '   ', rawLabel, uiLang: 'en' })).toBe(rawLabel)
    expect(pickToolLabel({ simplified: true, purpose: null, rawLabel, uiLang: 'en' })).toBe(rawLabel)
  })

  it('returns the purpose when its script matches the UI language', () => {
    expect(pickToolLabel({ simplified: true, purpose: 'Fetch the ghost node', rawLabel, uiLang: 'en' }))
      .toBe('Fetch the ghost node')
    expect(pickToolLabel({ simplified: true, purpose: '获取 ghost 节点', rawLabel, uiLang: 'zh-CN' }))
      .toBe('获取 ghost 节点')
  })

  it('falls back to the raw label when the purpose language does not match', () => {
    // The exact reported case: Chinese purpose lingering under an English UI.
    expect(pickToolLabel({ simplified: true, purpose: '获取第一个 Figma 节点的设计上下文', rawLabel, uiLang: 'en' }))
      .toBe(rawLabel)
  })

  it('trims the purpose before deciding', () => {
    expect(pickToolLabel({ simplified: true, purpose: '  Fetch the node  ', rawLabel, uiLang: 'en' }))
      .toBe('Fetch the node')
  })
})
