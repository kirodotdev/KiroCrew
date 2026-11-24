import { describe, expect, it } from 'vitest'
import { SpeechProbe, sessionNow, speechLine, type Recognition } from './speechProbe'

/** A recognition that a test drives by hand: it says what a browser's would say, when the test tells it to. */
class FakeRecognition implements Recognition {
  lang = ''
  continuous = true
  interimResults = false
  onstart: Recognition['onstart'] = null
  onresult: Recognition['onresult'] = null
  onspeechend: Recognition['onspeechend'] = null
  onerror: Recognition['onerror'] = null
  onend: Recognition['onend'] = null
  started = 0
  stopped = 0
  aborted = 0
  refuse = false
  start() { if (this.refuse) throw new DOMException('no', 'InvalidStateError'); this.started += 1 }
  stop() { this.stopped += 1 }
  abort() { this.aborted += 1 }
  hear(...parts: string[]) { this.onresult?.({ results: parts.map(transcript => [{ transcript }]) }) }
}

function probe() {
  const made: FakeRecognition[] = []
  const clock = { at: 0 }
  const changes = { count: 0 }
  const p = new SpeechProbe(() => { const r = new FakeRecognition(); made.push(r); return r }, () => clock.at)
  p.changed = () => { changes.count += 1 }
  return { p, made, clock, changes }
}

describe('the probe of the browser\'s speech recognition', () => {
  it('says that a browser without recognition has none, and starts nothing there', () => {
    const p = new SpeechProbe(null)
    expect(speechLine(p.state)).toBe('speech · this browser has no speech recognition')
    p.listen('en-US', true)
    expect(p.state.phase).toBe('none')
  })

  it('asks, listens, hears and ends, and says in capitals only while the microphone is on', () => {
    const { p, made, clock, changes } = probe()
    expect(speechLine(p.state)).toBe('speech · microphone off · press a Listen tile')
    p.listen('en-US', true)
    const rec = made[0]
    expect([rec.lang, rec.continuous, rec.interimResults, rec.started]).toEqual(['en-US', false, true, 1])
    expect(speechLine(p.state)).toBe('speech · asking for the microphone · en-US · glasses stayed')
    rec.onstart?.()
    expect(speechLine(p.state)).toBe('speech · MICROPHONE ON · en-US · hears: - · glasses stayed')
    rec.hear('hello ')
    expect(speechLine(p.state)).toBe('speech · MICROPHONE ON · en-US · hears: hello · glasses stayed')
    clock.at = 2000
    rec.onspeechend?.()
    clock.at = 2420
    rec.hear('hello ', 'Kiro Crew')
    rec.onend?.()
    expect(speechLine(p.state)).toBe('speech · microphone off · en-US · heard: hello Kiro Crew · 420 ms after the speech ended · glasses stayed')
    expect(p.busy).toBe(false)
    expect(changes.count).toBeGreaterThanOrEqual(5)
  })

  it('keeps the browser\'s own word for an error, and the microphone is off after it', () => {
    const { p, made } = probe()
    p.listen('zh-CN', false)
    made[0].onerror?.({ error: 'not-allowed' })
    // The browser's end after its error changes nothing more.
    made[0].onend?.()
    expect(speechLine(p.state)).toBe('speech · microphone off · zh-CN · error not-allowed · glasses none')
    expect(p.busy).toBe(false)
  })

  it('says an error when the browser refuses the start itself', () => {
    const made: FakeRecognition[] = []
    const p = new SpeechProbe(() => { const r = new FakeRecognition(); r.refuse = true; made.push(r); return r })
    p.listen('en-US', false)
    expect(p.state).toMatchObject({ phase: 'error', error: 'InvalidStateError' })
  })

  it('a second press while it runs stops it and starts no second one', () => {
    const { p, made } = probe()
    p.listen('en-US', true)
    made[0].onstart?.()
    p.listen('zh-CN', true)
    expect(made).toHaveLength(1)
    expect(made[0].stopped).toBe(1)
    expect(p.state.lang).toBe('en-US')
  })

  it('gives up a recognition that the browser never said it began, at the second press', () => {
    const { p, made } = probe()
    p.listen('en-US', true)
    p.listen('en-US', true)
    expect(made).toHaveLength(1)
    expect([made[0].aborted, made[0].stopped]).toEqual([1, 0])
    expect(speechLine(p.state)).toBe('speech · microphone off · en-US · error no-answer · glasses stayed')
    // What it says later is not heard, and the next press starts a new one.
    made[0].onstart?.()
    expect(p.state.phase).toBe('error')
    p.listen('zh-CN', true)
    expect(made).toHaveLength(2)
  })

  it('gives up a recognition that was told to stop and never said that it ended, at the next press', () => {
    const { p, made } = probe()
    p.listen('en-US', true)
    made[0].onstart?.()
    made[0].hear('hello')
    p.listen('en-US', true)
    expect([made[0].stopped, made[0].aborted]).toEqual([1, 0])
    expect(speechLine(p.state)).toContain('MICROPHONE ON')
    // The browser stays silent: the next press aborts it, and the line no longer says that the microphone is on.
    p.listen('en-US', true)
    expect([made[0].stopped, made[0].aborted]).toEqual([1, 1])
    expect(speechLine(p.state)).toBe('speech · microphone off · en-US · error no-end · glasses stayed')
    made[0].onend?.()
    expect(p.state.phase).toBe('error')
    p.listen('en-US', true)
    expect(made).toHaveLength(2)
  })

  it('remembers the worst that the session fared while the microphone was asked for or on: ended over went dark over blurred', () => {
    const { p, made } = probe()
    p.listen('en-US', true)
    made[0].onstart?.()
    p.sessionIs('visible')
    expect(p.state.session).toBe('stayed')
    // A prompt of the system over the session: the session runs, but it is not the same as one that stayed.
    p.sessionIs('blurred')
    p.sessionIs('visible')
    expect(p.state.session).toBe('blurred')
    expect(speechLine(p.state)).toBe('speech · MICROPHONE ON · en-US · hears: - · glasses blurred')
    p.sessionIs('hidden')
    p.sessionIs('blurred')
    p.sessionIs('visible')
    expect(p.state.session).toBe('hidden')
    p.sessionIs('ended')
    p.sessionIs('hidden')
    p.sessionIs('blurred')
    expect(p.state.session).toBe('ended')
    made[0].onend?.()
    expect(speechLine(p.state)).toBe('speech · microphone off · en-US · heard: - · - ms after the speech ended · glasses ended')
    // Outside a recognition nothing is noted, and with no session at the start there is none to lose.
    p.sessionIs('hidden')
    expect(p.state.session).toBe('ended')
    p.listen('en-US', false)
    p.sessionIs('ended')
    expect(p.state.session).toBeNull()
  })

  it('reads a session\'s own word: a blurred session is not one that stayed', () => {
    expect(sessionNow('visible')).toBe('visible')
    expect(sessionNow('visible-blurred')).toBe('blurred')
    expect(sessionNow('hidden')).toBe('hidden')
    expect(sessionNow(null)).toBe('ended')
    // A browser that gives a session no such word says nothing against it.
    expect(sessionNow(undefined)).toBe('visible')
  })

  it('stops the recognition when the session ends: once, and one that never began is given up', () => {
    const { p, made } = probe()
    p.listen('en-US', true)
    made[0].onstart?.()
    p.sessionIs('ended')
    p.sessionIs('ended')
    expect([made[0].stopped, made[0].aborted]).toEqual([1, 0])
    expect(speechLine(p.state)).toBe('speech · MICROPHONE ON · en-US · hears: - · glasses ended')
    made[0].onend?.()
    expect(p.busy).toBe(false)
    // Asked for and not yet begun when the session ends: there is nothing to wait for.
    p.listen('zh-CN', true)
    p.sessionIs('ended')
    expect(made[1].aborted).toBe(1)
    expect(speechLine(p.state)).toBe('speech · microphone off · zh-CN · error no-answer · glasses ended')
  })

  it('turns the microphone off when the part that drew it goes, and hears nothing from a recognition that was replaced', () => {
    const { p, made } = probe()
    p.listen('en-US', true)
    made[0].onstart?.()
    p.dispose()
    expect(made[0].aborted).toBe(1)
    expect(p.state.phase).toBe('off')
    made[0].hear('late words')
    made[0].onend?.()
    expect(p.state).toMatchObject({ phase: 'off', text: '' })
  })
})
