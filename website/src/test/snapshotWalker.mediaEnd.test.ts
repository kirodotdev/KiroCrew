/**
 * The walker's `media` read counts an ending as it happens (`mediaEnds`), so a site
 * that takes the source away right after the end (YouTube removes `src`: the element
 * then reads paused at 0 with no duration, never `ended`) or a poll that falls after
 * the moment still reports it once, as a new `endId`. An advert that hands over to
 * the item on the same page is no ending; the site moving on to another address is.
 *
 * happy-dom plays nothing: the element's playback fields are stubbed per element and
 * its media events dispatched by hand. A real Chromium playing a real file runs in
 * `test/test_browser_walker_media_end.py`.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const HARNESS_DIR = join(__dirname, '../../../src/kiro_crew/browser_harness')

function walkerSource(): string {
  const lines = readFileSync(join(HARNESS_DIR, 'snapshot_walker.js'), 'utf8').split('\n')
  while (lines.length && (!lines[0].trim() || lines[0].trimStart().startsWith('//'))) lines.shift()
  return lines.join('\n').trim()
}

type Media = { ok: boolean; ended: boolean; endId: string | null; paused: boolean; duration: number | null }
/** One evaluation of the walker per read, as the gateway makes it. */
const read = () => (new Function(`return (${walkerSource()})`)() as { media: (a: string) => Media }).media('state')

type Play = { paused: boolean; ended: boolean; currentTime: number; duration: number; readyState: number }
let play: Play
let video: HTMLVideoElement

function fire(type: string) {
  video.dispatchEvent(new Event(type))
}
/** The element playing at *t* of *d* seconds, as a `timeupdate` reports it. */
function at(t: number, d: number) {
  Object.assign(play, { paused: false, ended: false, currentTime: t, duration: d, readyState: 4 })
  fire('timeupdate')
}
/** What YouTube does at the end: drop the source, so the element is empty. */
function sourceTakenAway() {
  video.removeAttribute('src')
  Object.assign(play, { paused: true, ended: false, currentTime: 0, duration: NaN, readyState: 0 })
  fire('emptied')
}
function endedEvent() {
  Object.assign(play, { paused: true, ended: true, currentTime: play.duration })
  fire('ended')
}
function playing() {
  Object.assign(play, { paused: false, ended: false })
  fire('playing')
}
const later = (ms: number) => vi.setSystemTime(Date.now() + ms)

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(1_000_000)
  history.replaceState(null, '', '/watch?v=song-a')
  play = { paused: true, ended: false, currentTime: 0, duration: NaN, readyState: 0 }
  video = document.createElement('video')
  video.setAttribute('src', 'blob:song-a')
  for (const key of Object.keys(play) as (keyof Play)[]) {
    Object.defineProperty(video, key, { configurable: true, get: () => play[key] })
  }
  document.body.appendChild(video)
  // The listeners go in on the first read, as on a page the app's agent just opened.
  read()
})

afterEach(() => {
  vi.useRealTimers()
  document.body.replaceChildren()
  // A fresh "document" for the next test: the walker keys its state on `document`.
  const w = window as unknown as Record<string, unknown>
  delete w.__kcMedia
  delete w.__kcMediaDoc
})

describe('the walker counts a media ending once', () => {
  it('counts an ending whose source was taken away right after the end', () => {
    at(238.6, 239.5)
    sourceTakenAway()
    expect(read()).toMatchObject({ ok: true, ended: false, endId: null, duration: null })
    later(3000)
    const first = read()
    expect(first.endId).toMatch(/^[a-z0-9]+:1$/)
    // Reported as ended from now on, until something plays.
    expect(first).toMatchObject({ ended: true, paused: true, duration: null })
    later(1500)
    expect(read().endId).toBe(first.endId)
  })

  it('counts an `ended` that lasted shorter than the poll', () => {
    at(239.3, 239.5)
    endedEvent()
    sourceTakenAway()
    later(4500)
    expect(read()).toMatchObject({ ended: true, endId: expect.stringMatching(/:1$/) })
  })

  it('counts each new ending again after something played', () => {
    at(239.3, 239.5)
    endedEvent()
    later(3000)
    const first = read().endId
    playing()
    at(1, 180)
    expect(read()).toMatchObject({ ended: false, endId: first })
    at(179.8, 180)
    endedEvent()
    later(3000)
    const second = read().endId
    expect(second).toMatch(/:2$/)
    expect(second).not.toBe(first)
  })

  it('does not count an advert that hands over to the item on the same page', () => {
    at(14.8, 15)
    endedEvent()
    sourceTakenAway()
    later(800)
    video.setAttribute('src', 'blob:song-a-itself')
    playing()
    at(0.5, 239.5)
    later(5000)
    expect(read()).toMatchObject({ ended: false, endId: null })
  })

  it('counts the site moving on to its own next item at once', () => {
    at(239.4, 239.5)
    endedEvent()
    history.pushState(null, '', '/watch?v=song-b')
    playing()
    expect(read()).toMatchObject({ ended: false, endId: expect.stringMatching(/:1$/) })
  })

  it('does not count the source going away far from the end', () => {
    at(100, 239.5)
    sourceTakenAway()
    later(5000)
    expect(read()).toMatchObject({ ended: false, endId: null })
  })

  it('reads no ending while one waits out its grace, then one', () => {
    at(239.3, 239.5)
    endedEvent()
    // The element itself is `ended`, but nothing is counted yet: a reader's first
    // answer here must not call it an ending the later count names again.
    expect(read()).toMatchObject({ ended: false, endId: null })
    later(3000)
    expect(read()).toMatchObject({ ended: true, endId: expect.stringMatching(/:1$/) })
  })

  it('counts media already ended when the listeners go in, under one id', () => {
    const w = window as unknown as Record<string, unknown>
    delete w.__kcMedia
    delete w.__kcMediaDoc
    Object.assign(play, { paused: true, ended: true, currentTime: 200, duration: 200, readyState: 4 })
    const first = read()
    expect(first).toMatchObject({ ended: true, endId: expect.stringMatching(/:1$/) })
    later(5000)
    expect(read().endId).toBe(first.endId)
  })

  it('counts an ending whose grace passed before playback, with no read between', () => {
    at(239.3, 239.5)
    endedEvent()
    later(4000)
    playing()
    expect(read()).toMatchObject({ ended: false, endId: expect.stringMatching(/:1$/) })
  })

  it('ignores a smaller media element ending beside the main one', () => {
    const small = document.createElement('audio')
    Object.defineProperty(small, 'paused', { configurable: true, get: () => true })
    Object.defineProperty(small, 'ended', { configurable: true, get: () => true })
    small.getBoundingClientRect = () => ({ width: 10, height: 10 }) as DOMRect
    video.getBoundingClientRect = () => ({ width: 640, height: 360 }) as DOMRect
    document.body.appendChild(small)
    at(50, 239.5)
    small.dispatchEvent(new Event('ended'))
    later(5000)
    expect(read()).toMatchObject({ ended: false, endId: null })
  })

  it('does not count a paused main video near its end while a smaller preview plays', () => {
    video.getBoundingClientRect = () => ({ width: 640, height: 360 }) as DOMRect
    at(198.5, 200)
    read()
    play.paused = true
    const preview = document.createElement('video')
    Object.defineProperty(preview, 'paused', { configurable: true, get: () => false })
    Object.defineProperty(preview, 'ended', { configurable: true, get: () => false })
    Object.defineProperty(preview, 'duration', { configurable: true, get: () => 30 })
    Object.defineProperty(preview, 'currentTime', { configurable: true, get: () => 1 })
    Object.defineProperty(preview, 'readyState', { configurable: true, get: () => 4 })
    preview.getBoundingClientRect = () => ({ width: 160, height: 90 }) as DOMRect
    document.body.appendChild(preview)
    expect(read()).toMatchObject({ ended: false, endId: null })
    later(5000)
    expect(read()).toMatchObject({ ended: false, endId: null })
  })

  it('does not raise an ending again from an element a new one replaced', () => {
    Object.assign(play, { paused: false, ended: false, currentTime: 238.9, duration: 239.5, readyState: 4 })
    read()
    // The page swaps in a new element that starts the next part at once.
    video.remove()
    const next = document.createElement('video')
    Object.defineProperty(next, 'paused', { configurable: true, get: () => false })
    Object.defineProperty(next, 'duration', { configurable: true, get: () => 300 })
    Object.defineProperty(next, 'currentTime', { configurable: true, get: () => 0.4 })
    Object.defineProperty(next, 'readyState', { configurable: true, get: () => 4 })
    document.body.appendChild(next)
    next.dispatchEvent(new Event('playing'))
    read()
    later(5000)
    expect(read()).toMatchObject({ ended: false, endId: null })
  })

  it('counts an ending a read saw near the end when the events were missed', () => {
    Object.assign(play, { paused: false, ended: false, currentTime: 238.9, duration: 239.5, readyState: 4 })
    read()
    Object.assign(play, { paused: true, ended: false, currentTime: 0, duration: NaN, readyState: 0 })
    read()
    later(3000)
    expect(read().endId).toMatch(/:1$/)
  })
})
