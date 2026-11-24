import { describe, expect, it, vi } from 'vitest'
import { QUIET_MS, START_MS, langAfterRefusal, langName, listen, nextLang, speechHere, speechLangs, speechTag, startLang } from './chatSpeech'

/** A stand-in for the browser's recognizer: a test fires its events by hand. */
class Fake {
  static made: Fake[] = []
  lang = ''
  continuous = false
  interimResults = false
  onstart: (() => void) | null = null
  onresult: ((e: unknown) => void) | null = null
  onerror: ((e: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  started = 0
  stopped = 0
  constructor() { Fake.made.push(this) }
  start() { this.started += 1 }
  stop() { this.stopped += 1 }
  abort() { /* not used */ }
}
const result = (words: string, isFinal: boolean) => Object.assign([{ transcript: words }], { isFinal })
const hooks = () => ({ started: vi.fn(), heard: vi.fn(), none: vi.fn(), quiet: vi.fn() })

describe('the language a card listens in', () => {
  it('is Chinese or English, and the dashboard’s own when it is another', () => {
    expect(speechLangs('en')).toEqual(['zh-CN', 'en-US'])
    expect(speechLangs('zh-CN')).toEqual(['zh-CN', 'en-US'])
    expect(speechLangs('ja')).toEqual(['zh-CN', 'en-US', 'ja'])
    expect(speechLangs('pt-BR')).toEqual(['zh-CN', 'en-US', 'pt-BR'])
    expect(speechLangs('not a tag!')).toEqual(['zh-CN', 'en-US'])
    expect(speechTag('en-XA')).toBe('en-US')
    expect(speechTag('zh-TW')).toBe('zh-CN')
    expect(speechTag('zhx')).toBe('zhx')
  })

  it('starts as the dashboard’s, or as the one kept in this browser when that is one of the list', () => {
    expect(startLang(null, 'en')).toBe('en-US')
    expect(startLang(null, 'zh-CN')).toBe('zh-CN')
    expect(startLang(null, 'ja')).toBe('ja')
    expect(startLang('zh-CN', 'en')).toBe('zh-CN')
    expect(startLang('ja', 'en')).toBe('en-US')
    expect(startLang('<script>', 'en')).toBe('en-US')
    expect(startLang(null, '')).toBe('zh-CN')
  })

  it('goes to the next at a pinch, and round again', () => {
    const list = speechLangs('ja')
    expect(nextLang('zh-CN', list)).toBe('en-US')
    expect(nextLang('en-US', list)).toBe('ja')
    expect(nextLang('ja', list)).toBe('zh-CN')
    expect(nextLang('gone', list)).toBe('zh-CN')
  })

  it('falls back to the first of the list when the recognizer does not know it, and to none when that was the first', () => {
    const list = speechLangs('ja')
    expect(langAfterRefusal('ja', list)).toBe('zh-CN')
    expect(langAfterRefusal('en-US', list)).toBe('zh-CN')
    expect(langAfterRefusal('zh-CN', list)).toBeNull()
    expect(langAfterRefusal('ja', [])).toBeNull()
  })

  it('a language that the recognizer does not know is said as that, once, and not as "no recognition here"', () => {
    Fake.made = []
    const on = { ...hooks(), noLang: vi.fn() }
    listen('ja', on, { SpeechRecognition: Fake })
    Fake.made[0].onerror?.({ error: 'language-not-supported' })
    Fake.made[0].onend?.()
    expect(on.noLang).toHaveBeenCalledTimes(1)
    expect(on.none).not.toHaveBeenCalled()
    // It is stopped: its end starts no other.
    expect(Fake.made).toHaveLength(1)
    // A caller that does not ask for it hears `none`, as for any recognizer that does not work.
    Fake.made = []
    const plain = hooks()
    listen('ja', plain, { SpeechRecognition: Fake })
    Fake.made[0].onerror?.({ error: 'language-not-supported' })
    expect(plain.none).toHaveBeenCalledTimes(1)
  })

  it('shows each language by its own name', () => {
    expect(langName('zh-CN')).toBe('中文')
    expect(langName('en-US')).toBe('English')
    expect(langName('ja')).toBe('日本語')
    expect(langName('de')).toBe('Deutsch')
  })
})

describe('speech to text for the card', () => {
  it('says there is none where the browser has no recognizer', () => {
    expect(speechHere({})).toBe(false)
    expect(speechHere({ webkitSpeechRecognition: Fake })).toBe(true)
    expect(listen('en', hooks(), {})).toBeNull()
  })

  it('listens in the given language, without end, with the words still worked on', () => {
    Fake.made = []
    const on = hooks()
    const heard = listen('zh-CN', on, { SpeechRecognition: Fake })
    const rec = Fake.made[0]
    expect(rec).toMatchObject({ lang: 'zh-CN', continuous: true, interimResults: true, started: 1 })
    rec.onstart?.()
    expect(on.started).toHaveBeenCalledTimes(1)
    rec.onresult?.({ resultIndex: 0, results: [result('ship the', false)] })
    rec.onresult?.({ resultIndex: 0, results: [result('ship the fix', true), result('to', false), result('day', false)] })
    expect(on.heard.mock.calls).toEqual([['', 'ship the'], ['ship the fix', 'today']])
    heard?.stop()
    expect(rec.stopped).toBe(1)
    // After the stop nothing more is told, and it is not started again.
    rec.onresult?.({ resultIndex: 0, results: [result('late', true)] })
    rec.onend?.()
    expect(on.heard).toHaveBeenCalledTimes(2)
    expect(Fake.made).toHaveLength(1)
  })

  it('starts again when the recognizer ends by itself after a silence', () => {
    Fake.made = []
    let t = 0
    listen('en', hooks(), { SpeechRecognition: Fake }, () => t)
    t = 5000
    Fake.made[0].onend?.()
    expect(Fake.made).toHaveLength(2)
    expect(Fake.made[1].started).toBe(1)
  })

  it('gives up once when the microphone or the service is refused', () => {
    Fake.made = []
    const on = hooks()
    listen('en', on, { SpeechRecognition: Fake })
    Fake.made[0].onerror?.({ error: 'no-speech' })
    expect(on.none).not.toHaveBeenCalled()
    Fake.made[0].onerror?.({ error: 'not-allowed' })
    Fake.made[0].onend?.()
    expect(on.none).toHaveBeenCalledTimes(1)
    expect(Fake.made).toHaveLength(1)
  })

  it('gives up on a recognizer that never says that it listens, and not on one that does', () => {
    vi.useFakeTimers()
    try {
      Fake.made = []
      const silent = hooks()
      listen('en', silent, { SpeechRecognition: Fake })
      vi.advanceTimersByTime(START_MS - 1)
      expect(silent.none).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1)
      expect(silent.none).toHaveBeenCalledTimes(1)
      const fine = hooks()
      listen('en', fine, { SpeechRecognition: Fake })
      Fake.made[1].onstart?.()
      vi.advanceTimersByTime(START_MS * 2)
      expect(fine.none).not.toHaveBeenCalled()
      const stopped = hooks()
      listen('en', stopped, { SpeechRecognition: Fake })?.stop()
      vi.advanceTimersByTime(START_MS * 2)
      expect(stopped.none).not.toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('is stopped after 20 s with no words, counted from when it first listens and anew from each word', () => {
    vi.useFakeTimers()
    try {
      Fake.made = []
      const on = hooks()
      listen('en-US', on, { SpeechRecognition: Fake }, () => 0)
      const rec = Fake.made[0]
      // Not yet listening: the quiet time has not begun (the start's own wait runs).
      vi.advanceTimersByTime(START_MS - 1)
      expect(on.quiet).not.toHaveBeenCalled()
      rec.onstart?.()
      vi.advanceTimersByTime(QUIET_MS - 1)
      expect(on.quiet).not.toHaveBeenCalled()
      // A word starts it anew; a result with no words does not.
      rec.onresult?.({ resultIndex: 0, results: [result('hello', false)] })
      vi.advanceTimersByTime(QUIET_MS - 1)
      rec.onresult?.({ resultIndex: 0, results: [result('  ', false)] })
      expect(on.quiet).not.toHaveBeenCalled()
      vi.advanceTimersByTime(1)
      expect(on.quiet).toHaveBeenCalledTimes(1)
      expect(rec.stopped).toBe(1)
      // Stopped: it is not started again, and nothing more is told.
      rec.onend?.()
      rec.onresult?.({ resultIndex: 0, results: [result('late', true)] })
      expect(Fake.made).toHaveLength(1)
      expect(on.heard).toHaveBeenCalledTimes(2)
      expect(on.none).not.toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('counts the quiet time across the recognizer’s own restarts, and not after a stop', () => {
    vi.useFakeTimers()
    try {
      Fake.made = []
      let t = 0
      const on = hooks()
      listen('en-US', on, { SpeechRecognition: Fake }, () => t)
      Fake.made[0].onstart?.()
      vi.advanceTimersByTime(12_000)
      t = 12_000
      Fake.made[0].onend?.()
      Fake.made[1].onstart?.()
      vi.advanceTimersByTime(QUIET_MS - 12_000)
      expect(on.quiet).toHaveBeenCalledTimes(1)
      const stopped = hooks()
      const heard = listen('en-US', stopped, { SpeechRecognition: Fake })
      Fake.made[2].onstart?.()
      heard?.stop()
      vi.advanceTimersByTime(QUIET_MS * 2)
      expect(stopped.quiet).not.toHaveBeenCalled()
    } finally { vi.useRealTimers() }
  })

  it('gives up when the recognizer ends at once three times in a row, or cannot start', () => {
    Fake.made = []
    const on = hooks()
    listen('en', on, { SpeechRecognition: Fake }, () => 0)
    Fake.made[0].onend?.()
    Fake.made[1].onend?.()
    expect(on.none).not.toHaveBeenCalled()
    Fake.made[2].onend?.()
    expect(on.none).toHaveBeenCalledTimes(1)
    const broken = hooks()
    class Throws extends Fake { start() { throw new Error('busy') } }
    listen('en', broken, { SpeechRecognition: Throws })
    expect(broken.none).toHaveBeenCalledTimes(1)
  })
})
